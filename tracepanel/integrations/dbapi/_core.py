"""What every driver shares: who called it, recording the statement, wrapping a cursor class, EXPLAIN."""

import functools
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from tracepanel.core import explain as plans
from tracepanel.core import sql
from tracepanel.core.context import current_trace, now_us
from tracepanel.core.model import Attrs, Scalar
from tracepanel.core.patching import patch
from tracepanel.core.services import database

T = TypeVar("T")

# This package's wrappers: the frames between the driver's caller and the statement.
_SELF = __name__.rpartition(".")[0]
# The layers that record their own statements (or run internal ones): a driver call they make is theirs.
LAYERS = ("django.db.", "sqlalchemy.", "tracepanel.", "aiosqlite.", "psycopg_pool.")
# The drivers' own modules (psycopg's Connection.execute calls its cursor's): passed over to find the caller.
DRIVERS = ("sqlite3", "_sqlite3", "psycopg", "asyncpg", "pymysql", "MySQLdb", "mysql.connector", "aiomysql", "oracledb")
# The drivers' housekeeping: statements they run themselves (asyncpg's pool resets a connection it gets back).
HOUSEKEEPING = frozenset({("asyncpg.connection", "reset"), ("asyncpg.connection", "_reset")})
# A bound on the frames passed over; the caller is a few frames up.
_DEPTH = 32


def app_called(frame: Any) -> bool:  # noqa: ANN401 (a frame)
    """Whether the statement is the app's to record here, looking up the stack from the wrapper's frame.

    Not when an ORM (or tracepanel) called the driver: it records the statement itself. Not when a wrapper further out
    is already recording it: the driver calling its own wrapped method (pymysql's executemany calls execute).
    """
    while frame is not None and frame.f_globals.get("__name__", "").startswith(_SELF):
        frame = frame.f_back
    for _ in range(_DEPTH):
        if frame is None:
            return True
        name: str = frame.f_globals.get("__name__", "")
        if name.startswith(_SELF) or name.startswith(LAYERS):
            return False
        if not name.startswith(DRIVERS):
            return True
        if (name, frame.f_code.co_name) in HOUSEKEEPING:
            return False
        frame = frame.f_back
    return True


def run(execute: Callable[[], T], statement: object, params: sql.Params, *, many: bool, db: Callable[[], Attrs]) -> T:
    """Run a driver call, recorded as an `sql` span when the app made it inside a trace."""
    if current_trace() is None or not app_called(sys._getframe(1)):  # noqa: SLF001 (the stack tells who called)
        return execute()
    return sql.run(execute, text(statement), params, many=many, database=db)


async def run_async(
    execute: Callable[[], Awaitable[T]], statement: object, params: sql.Params, *, many: bool, db: Callable[[], Attrs]
) -> T:
    """run() for an async driver: the check runs once the call is awaited, where the stack shows who awaits it."""
    if current_trace() is None or not app_called(sys._getframe(1)):  # noqa: SLF001
        return await execute()
    start, counter, status = now_us(), time.perf_counter_ns(), "ok"
    try:
        return await execute()
    except Exception:
        status = "error"
        raise
    finally:
        sql.record(
            text(statement),
            params,
            many=many,
            database=db,
            start=start,
            duration=(time.perf_counter_ns() - counter) // 1000,
            status=status,
        )


def text(statement: object) -> str:
    if isinstance(statement, bytes):
        return statement.decode(errors="replace")
    return str(statement)


def params_of(params: object) -> sql.Params:
    """One statement's parameters as tracepanel.core.sql takes them: a sequence or a mapping, else none."""
    if params is None or isinstance(params, (str, bytes)):
        return None
    if isinstance(params, Mapping):
        return dict(params)
    if isinstance(params, Sequence):
        return tuple(params)
    return None


# ---------- wrapping ----------

# How a wrapped method's arguments read: (statement, parameters, many) from its positional and keyword arguments.
Reads = Callable[[tuple[Any, ...], dict[str, Any]], tuple[object, sql.Params, bool]]


def dbapi_execute(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """execute(operation, parameters=None): DB-API 2.0."""
    statement = args[0] if args else kwargs.get("query", kwargs.get("operation", kwargs.get("sql", "")))
    params = args[1] if len(args) > 1 else kwargs.get("params", kwargs.get("args", kwargs.get("parameters")))
    return statement, params_of(params), False


def dbapi_executemany(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """executemany(operation, seq_of_parameters): one statement, its parameters many rows."""
    statement = args[0] if args else kwargs.get("query", kwargs.get("operation", kwargs.get("sql", "")))
    return statement, None, True


def dbapi_script(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """executescript(sql_script): several statements in one call, recorded as one."""
    return (args[0] if args else kwargs.get("sql_script", "")), None, False


DBAPI: dict[str, Reads] = {"execute": dbapi_execute, "executemany": dbapi_executemany}


def defined_on(cls: type, name: str) -> bool:
    """Whether the class defines the method itself: an inherited one is wrapped once, on the class that defines it."""
    return name in vars(cls)


def wrap(
    cls: type,
    methods: Mapping[str, Reads],
    *,
    db: Callable[[Any], Attrs],
    statement: Callable[[object, Any], object] = lambda query, _owner: query,
    asynchronous: bool = False,
) -> None:
    """Record the statements of these methods of a Python class (defined on it), sync or async.

    `db` describes where an instance's statements run; `statement` turns a query object into its text (psycopg's
    sql.Composed renders on the cursor).
    """
    for name, reads in methods.items():
        if not defined_on(cls, name):
            continue
        method = vars(cls)[name]
        patch(
            cls,
            name,
            _async_wrapper(method, reads, db, statement) if asynchronous else _wrapper(method, reads, db, statement),
        )


def _wrapper(
    method: Callable[..., Any], reads: Reads, db: Callable[[Any], Attrs], statement: Callable[[object, Any], object]
) -> Callable[..., Any]:
    @functools.wraps(method)
    def traced(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (the driver's own signature)
        query, params, many = reads(args, kwargs)
        return run(
            lambda: method(self, *args, **kwargs), statement(query, self), params, many=many, db=lambda: db(self)
        )

    return traced


def _async_wrapper(
    method: Callable[..., Any], reads: Reads, db: Callable[[Any], Attrs], statement: Callable[[object, Any], object]
) -> Callable[..., Any]:
    @functools.wraps(method)
    async def traced(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        query, params, many = reads(args, kwargs)
        return await run_async(
            lambda: method(self, *args, **kwargs), statement(query, self), params, many=many, db=lambda: db(self)
        )

    return traced


@functools.cache
def subclass(base: type, mixin: type) -> type:
    """The base class with the mixin in front of it, for drivers written in C; one already traced is kept."""
    if not isinstance(base, type) or issubclass(base, mixin):
        return base
    return type(f"Traced{base.__name__}", (mixin, base), {"__module__": base.__module__})


# ---------- where a statement ran, and EXPLAIN there ----------


@dataclass(frozen=True)
class Where:
    """Where a driver's connection points: its vendor, server and database, and how to open another for EXPLAIN."""

    client: str
    vendor: str
    host: str = ""
    port: str = ""
    name: str = ""
    # Opens a new connection to the same database: (sql, params) -> (rows, columns). None: no EXPLAIN for this one.
    explain: plans.Execute | None = None


# Explainers by db.alias: the last connection seen under each alias says how to reach its database.
_explainers: dict[str, tuple[str, plans.Execute | None]] = {}
_lock = threading.Lock()


def attrs(where: Where) -> Attrs:
    """A statement's database attributes; its alias is the client and the database (`psycopg:shop`)."""
    named = database(where.vendor, where.host, where.port, where.name)
    # A SQLite file by its name; an in-memory database (no namespace) and a server's unnamed one by the vendor.
    label = named.get("db.namespace") if where.vendor == "sqlite" else where.name
    alias = f"{where.client}:{label or where.vendor}"
    with _lock:
        _explainers[alias] = (where.vendor, where.explain)
    return {
        "db.client": where.client,
        "db.alias": alias,
        "db.vendor": where.vendor,
        **named,
    }


def explain(
    statement: str, params: Sequence[Scalar] | Mapping[str, Scalar] | None, alias: str, *, analyze: bool = False
) -> plans.Plan:
    """EXPLAIN on a new connection to the database a driver's statement ran on."""
    with _lock:
        known = _explainers.get(alias)
    if known is None:
        msg = f"No database {alias!r} in this process."
        raise plans.ExplainError(msg)
    vendor, execute = known
    if execute is None:
        msg = f"EXPLAIN isn't available for queries made through {alias.partition(':')[0]}."
        raise plans.ExplainError(msg)

    def guarded(sql_text: str, values: Sequence[Scalar] | Mapping[str, Scalar] | None) -> tuple[plans.Rows, list[str]]:
        try:
            return execute(sql_text, values)
        except plans.ExplainError:
            raise
        except Exception as exc:
            msg = f"The database could not explain this query: {exc}"
            raise plans.ExplainError(msg) from exc

    return plans.explain(statement, params, vendor=vendor, alias=alias, execute=guarded, analyze=analyze)


def known(alias: str) -> bool:
    with _lock:
        return alias in _explainers


def cursor_rows(cursor: Any, sql_text: str, values: object) -> tuple[plans.Rows, list[str]]:  # noqa: ANN401
    """Run a statement on a DB-API cursor: its rows and column names."""
    cursor.execute(sql_text, values if values is not None else ())
    rows = [tuple(row) for row in cursor.fetchall()]
    return rows, [str(column[0]) for column in cursor.description or []]
