"""Database drivers used directly, without an ORM: sqlite3 and psycopg (3).

The third database layer on tracepanel.core.sql (Django's ORM and SQLAlchemy are the others): the statement, its
parameters, its place in project code and the N+1 / duplicate analysis are the same. An app that talks to its database
through the driver (FastAPI or Flask with raw SQL, a script) gets its queries traced like an ORM's.

An ORM runs its statements through the same drivers, and records them itself. What decides is who called the driver:
the first frame above the call that is neither the driver's own code nor this module's. When that is Django's database
layer, SQLAlchemy or tracepanel itself (EXPLAIN), the statement is theirs and the wrapper leaves it alone; the app, or a
library on top of the driver, gets it recorded here.

- sqlite3: its classes are C types, so connect() gets a Connection subclass whose cursors are traced; a factory the
  app (or Django) passes is subclassed rather than replaced.
- psycopg (3): its cursors are Python classes, patched in place, sync and async.
"""

import contextlib
import functools
import importlib
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from tracepanel.core import sql
from tracepanel.core.context import current_trace, now_us
from tracepanel.core.model import Attrs
from tracepanel.core.patching import original, patch
from tracepanel.core.services import database

T = TypeVar("T")

# The layers that record their own statements: a driver call they make is theirs.
_LAYERS = ("django.db.", "sqlalchemy.", "tracepanel.")
# The drivers' own modules (psycopg's Connection.execute calls its cursor's): passed over to find the caller.
_DRIVERS = ("sqlite3", "_sqlite3", "psycopg")
# A bound on the frames passed over; the caller is a few frames up.
_DEPTH = 24


def _from_a_layer() -> bool:
    """Whether an ORM (or tracepanel) called the driver, rather than the app or a library of the app's."""
    frame = sys._getframe(1)  # noqa: SLF001 (the stack is what tells the app's call from an ORM's)
    for _ in range(_DEPTH):
        if frame is None:
            return False
        name: str = frame.f_globals.get("__name__", "")
        if name != __name__ and not name.startswith(_DRIVERS):
            return name.startswith(_LAYERS)
        frame = frame.f_back
    return False


def _record(execute: Callable[[], T], statement: object, params: object, *, many: bool, db: Callable[[], Attrs]) -> T:
    if current_trace() is None or _from_a_layer():
        return execute()
    return sql.run(execute, _text(statement), _params(params, many=many), many=many, database=db)


async def _record_async(
    execute: Callable[[], Awaitable[T]], statement: object, params: object, *, many: bool, db: Callable[[], Attrs]
) -> T:
    if current_trace() is None or _from_a_layer():
        return await execute()
    start, counter, status = now_us(), time.perf_counter_ns(), "ok"
    try:
        return await execute()
    except Exception:
        status = "error"
        raise
    finally:
        sql.record(
            _text(statement),
            _params(params, many=many),
            many=many,
            database=db,
            start=start,
            duration=(time.perf_counter_ns() - counter) // 1000,
            status=status,
        )


def _text(statement: object) -> str:
    if isinstance(statement, bytes):
        return statement.decode(errors="replace")
    return str(statement)


def _params(params: object, *, many: bool) -> sql.Params:
    """The statement's parameters as tracepanel.core.sql takes them; executemany's sequence isn't one statement's."""
    if many or params is None or isinstance(params, (str, bytes)):
        return None
    if isinstance(params, dict):
        return params
    try:
        return tuple(params)  # type: ignore[call-overload]
    except TypeError:
        return None


# ---------- sqlite3 ----------

_SQLITE_PATH = "_tracepanel_database"
# connect(database, timeout, detect_types, isolation_level, check_same_thread, factory, ...): factory's position.
_FACTORY_POSITION = 5


def _sqlite_db(connection: object) -> Callable[[], Attrs]:
    def attrs() -> Attrs:
        path = str(getattr(connection, _SQLITE_PATH, "") or "")
        return {"db.client": "sqlite3", "db.alias": path.rsplit("/", 1)[-1] or "sqlite", "db.vendor": "sqlite"} | (
            database("sqlite", "", "", path)
        )

    return attrs


class _SqliteCursor:
    """Mixed into a sqlite3.Cursor class: its statements are recorded."""

    def execute(self, *args: Any) -> Any:  # noqa: ANN401 (sqlite3's own signature, passed on unchanged)
        run = functools.partial(super().execute, *args)  # type: ignore[misc]
        return _record(run, args[0], args[1] if len(args) > 1 else None, many=False, db=_sqlite_db(self.connection))  # type: ignore[attr-defined]

    def executemany(self, *args: Any) -> Any:  # noqa: ANN401
        run = functools.partial(super().executemany, *args)  # type: ignore[misc]
        return _record(run, args[0], None, many=True, db=_sqlite_db(self.connection))  # type: ignore[attr-defined]

    def executescript(self, *args: Any) -> Any:  # noqa: ANN401
        run = functools.partial(super().executescript, *args)  # type: ignore[misc]
        return _record(run, args[0], None, many=False, db=_sqlite_db(self.connection))  # type: ignore[attr-defined]


class _SqliteConnection:
    """Mixed into a sqlite3.Connection class: its cursors, and its execute() shortcuts, are recorded."""

    def cursor(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        import sqlite3  # noqa: PLC0415 (only once sqlite3 is in use)

        factory = kwargs.pop("factory", args[0] if args else sqlite3.Cursor)
        return super().cursor(_traced(factory, _SqliteCursor))  # type: ignore[misc]

    # As sqlite3 documents them: a new cursor, and its method called.
    def execute(self, *args: Any) -> Any:  # noqa: ANN401
        return self.cursor().execute(*args)

    def executemany(self, *args: Any) -> Any:  # noqa: ANN401
        return self.cursor().executemany(*args)

    def executescript(self, *args: Any) -> Any:  # noqa: ANN401
        return self.cursor().executescript(*args)


@functools.cache
def _traced(base: type, mixin: type) -> type:
    """The base class with the mixin in front of it; one already traced, or not a class, is kept as it is."""
    if not isinstance(base, type) or issubclass(base, mixin):
        return base
    return type(f"Traced{base.__name__}", (mixin, base), {"__module__": __name__})


def _sqlite_connect(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (sqlite3.connect's signature)
    import sqlite3  # noqa: PLC0415

    # factory is connect()'s sixth positional parameter, or a keyword.
    positional = list(args)
    factory = kwargs.pop(
        "factory",
        positional.pop(_FACTORY_POSITION) if len(positional) > _FACTORY_POSITION else sqlite3.Connection,
    )
    connection = original(sqlite3, "connect")(*positional, factory=_traced(factory, _SqliteConnection), **kwargs)
    path = positional[0] if positional else kwargs.get("database", "")
    # A factory with __slots__ takes no path: the statements are recorded all the same.
    with contextlib.suppress(AttributeError):
        setattr(connection, _SQLITE_PATH, str(path.decode() if isinstance(path, bytes) else path))
    return connection


def _install_sqlite3() -> None:
    import sqlite3  # noqa: PLC0415
    import sqlite3.dbapi2  # noqa: PLC0415

    patch(sqlite3, "connect", _sqlite_connect)
    # Django's backend and others import the driver as sqlite3.dbapi2.
    patch(sqlite3.dbapi2, "connect", _sqlite_connect)


# ---------- psycopg (3) ----------


def _psycopg_db(cursor: Any) -> Callable[[], Attrs]:  # noqa: ANN401 (psycopg.Cursor)
    def attrs() -> Attrs:
        info = cursor.connection.info
        name = str(info.dbname or "")
        return {"db.client": "psycopg", "db.alias": name or "postgresql", "db.vendor": "postgresql"} | database(
            "postgresql", str(info.host or ""), str(info.port or ""), name
        )

    return attrs


def _psycopg_text(query: object, cursor: Any) -> object:  # noqa: ANN401
    """A psycopg.sql composition as the server gets it; a string or bytes as it is."""
    as_string = getattr(query, "as_string", None)
    if as_string is None:
        return query
    try:
        return as_string(cursor)
    except Exception:  # noqa: BLE001 (a composition that can't render is still the app's statement)
        return str(query)


def _own(cls: type, name: str) -> bool:
    """Whether the class defines the method itself: an inherited one is wrapped once, on the class that defines it."""
    return name in vars(cls)


def _patch_psycopg_sync(cls: type) -> None:
    execute, executemany = cls.execute, cls.executemany  # type: ignore[attr-defined]

    def traced_execute(self: Any, query: object, params: object = None, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        run = functools.partial(execute, self, query, params, *args, **kwargs)
        return _record(run, _psycopg_text(query, self), params, many=False, db=_psycopg_db(self))

    def traced_executemany(self: Any, query: object, params_seq: object, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        run = functools.partial(executemany, self, query, params_seq, *args, **kwargs)
        return _record(run, _psycopg_text(query, self), None, many=True, db=_psycopg_db(self))

    if _own(cls, "execute"):
        patch(cls, "execute", traced_execute)
    if _own(cls, "executemany"):
        patch(cls, "executemany", traced_executemany)


def _patch_psycopg_async(cls: type) -> None:
    execute, executemany = cls.execute, cls.executemany  # type: ignore[attr-defined]

    async def traced_execute(self: Any, query: object, params: object = None, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        run = functools.partial(execute, self, query, params, *args, **kwargs)
        return await _record_async(run, _psycopg_text(query, self), params, many=False, db=_psycopg_db(self))

    async def traced_executemany(self: Any, query: object, params_seq: object, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        run = functools.partial(executemany, self, query, params_seq, *args, **kwargs)
        return await _record_async(run, _psycopg_text(query, self), None, many=True, db=_psycopg_db(self))

    if _own(cls, "execute"):
        patch(cls, "execute", traced_execute)
    if _own(cls, "executemany"):
        patch(cls, "executemany", traced_executemany)


def _install_psycopg() -> None:
    try:
        psycopg = importlib.import_module("psycopg")  # optional: installed with the app's Postgres
    except ImportError:
        return
    # ServerCursor and AsyncServerCursor define their own execute(); the others inherit Cursor's and AsyncCursor's.
    for name in ("Cursor", "ServerCursor"):
        if hasattr(psycopg, name):
            _patch_psycopg_sync(getattr(psycopg, name))
    for name in ("AsyncCursor", "AsyncServerCursor"):
        if hasattr(psycopg, name):
            _patch_psycopg_async(getattr(psycopg, name))


def install() -> None:
    """Wrap the drivers that are installed: sqlite3 always (the standard library), psycopg when it is there."""
    _install_sqlite3()
    _install_psycopg()
