"""SQLAlchemy: every statement of every engine as an `sql` span, every transaction as a `transaction` span.

A connection its pool opens is a `sql.connect` span (a server's: a SQLite file opens in no time).

The second database layer on tracepanel.core.sql (Django's ORM is the first): the statement, its parameters for EXPLAIN,
its place in project code and the N+1 / duplicate analysis are the same. Through SQLAlchemy's own events, so whatever
runs on it is covered (sqlakit, SQLModel, Flask-SQLAlchemy). Turned on when SQLAlchemy is installed.

Several databases: each engine gets its own `db.alias`: its `logging_name` when set (`create_engine(url,
logging_name="replica")`), else the database's name, made unique with the host (or a number) when another engine
already has it. EXPLAIN runs on the engine of that alias (an async engine's on a connection of its own).
"""

import asyncio
import functools
import sys
import time
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from tracepanel.core import explain as plans
from tracepanel.core import forks, sql
from tracepanel.core.context import (
    OpenSpan,
    adopt_held,
    current_span,
    current_trace,
    hold,
    now_us,
    open_span,
    release,
)
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, Scalar, Span, Trace
from tracepanel.core.patching import patch
from tracepanel.core.services import database, system

MARKER = "_tracepanel"
# Where a statement's timing waits between before_cursor_execute and after_cursor_execute.
_STARTED = "_tracepanel_started"


def _database(connection: Any) -> Attrs:  # noqa: ANN401 (sqlalchemy.engine.Connection)
    return _engine_database(connection.engine)


def _engine_database(engine: Any) -> Attrs:  # noqa: ANN401 (sqlalchemy.engine.Engine)
    url = engine.url
    vendor = str(engine.dialect.name)
    return {
        "db.client": "sqlalchemy",
        "db.alias": engine_alias(engine),
        "db.vendor": vendor,
        **database(vendor, url.host or "", str(url.port or ""), url.database or ""),
    }


# Engines by the db.alias their statements carry (EXPLAIN runs on the one a query came from), and back.
_engines: "weakref.WeakValueDictionary[str, Any]" = weakref.WeakValueDictionary()
_aliases: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()
_lock = forks.lock()


def engine_alias(engine: Any) -> str:  # noqa: ANN401 (sqlalchemy.engine.Engine)
    """A name for the engine, unique among this process's engines (there are no DATABASES aliases here)."""
    known = _aliases.get(engine)
    if known is not None:
        return known
    with _lock:
        url, vendor = engine.url, str(engine.dialect.name)
        named = getattr(engine, "logging_name", None)
        database = Path(url.database).name if vendor == "sqlite" and url.database else url.database
        base = str(named or database or vendor)
        alias = base
        if alias in _engines and _engines[alias] is not engine:
            alias = f"{base}@{url.host}" if url.host else base
            number = 2
            while alias in _engines and _engines[alias] is not engine:
                alias, number = f"{base}#{number}", number + 1
        _engines[alias] = engine
        _aliases[engine] = alias
        return alias


def explain(
    statement: str, params: Sequence[Scalar] | Mapping[str, Scalar] | None, alias: str, *, analyze: bool = False
) -> plans.Plan:
    """EXPLAIN on the engine the statement ran on, through its DB-API connection (parameters as the cursor got them)."""
    engine = _engines.get(alias)
    if engine is None:
        msg = f"No engine for database {alias!r} in this process."
        raise plans.ExplainError(msg)

    def execute(sql_text: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
        try:
            if engine.dialect.is_async:
                return _explain_async(engine, sql_text, values, session)
            connection = engine.raw_connection()
            try:
                return plans.cursor_rows(connection.cursor(), sql_text, values, session)
            finally:
                connection.close()
        except Exception as exc:
            msg = f"The database could not explain this query: {exc}"
            raise plans.ExplainError(msg) from exc

    return plans.explain(
        statement, params, vendor=str(engine.dialect.name), alias=alias, execute=execute, analyze=analyze
    )


def _explain_async(
    engine: Any,  # noqa: ANN401 (sqlalchemy.engine.Engine: an AsyncEngine's sync_engine)
    sql_text: str,
    values: plans.Values,
    session: plans.Session,
) -> tuple[plans.Rows, list[str]]:
    """EXPLAIN on an async driver (aiosqlite, asyncpg, asyncmy...): on a connection of its own, in a loop of its own.

    The driver's calls only run inside greenlet_spawn, on an event loop. The console's thread has none: the statement
    runs in a new one (asyncio.run), on a connection made for it by a copy of the engine's pool (the same URL, connect
    arguments and connect events) and closed after. Never the app's pool or loop: asyncpg's connections belong to the
    loop they were made on, and the app's loop may be busy or gone. An in-memory SQLite database is the connection
    its pool keeps (StaticPool): that one is borrowed, as aiosqlite's connections belong to no loop.
    """
    from sqlalchemy.pool import StaticPool  # noqa: PLC0415 (optional dependency)
    from sqlalchemy.util import greenlet_spawn  # noqa: PLC0415

    shared = engine.dialect.name == "sqlite" and isinstance(engine.pool, StaticPool)
    pool = engine.pool if shared else engine.pool.recreate()

    def run() -> tuple[plans.Rows, list[str]]:
        try:
            connection = pool.connect()
            try:
                return plans.cursor_rows(connection.cursor(), sql_text, values, session)
            finally:
                connection.close()
        finally:
            if not shared:
                pool.dispose()

    async def explain() -> tuple[plans.Rows, list[str]]:
        return await greenlet_spawn(run)

    return asyncio.run(explain())


def _before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:  # noqa: ANN401, ARG001, FBT001, PLR0913, PLR0917 (SQLAlchemy's signature)
    if current_trace() is not None and context is not None:
        # What the driver records meanwhile (a dialect over HTTP: Snowflake's, Trino's) is the statement's.
        setattr(context, _STARTED, (now_us(), time.perf_counter_ns(), hold()))


def _finish(conn: Any, statement: str, parameters: Any, context: Any, *, many: bool, status: str) -> None:  # noqa: ANN401, PLR0913
    started = getattr(context, _STARTED, None)
    if started is None:
        return
    start, counter, held = started
    setattr(context, _STARTED, None)
    inside = release(held)
    span = sql.record(
        statement,
        parameters,
        many=many,
        database=lambda: _database(conn),
        start=start,
        duration=(time.perf_counter_ns() - counter) // 1000,
        status=status,
        parent=_transaction(conn),
    )
    adopt_held(held, inside, span)
    if span is not None and status == "ok":
        # The result made from this context counts what is fetched from it.
        sql.counts_into(context, span)


# CursorResult's fetches, which every way of reading a result (the ORM's too) goes through: the rows they return count
# into the span of the statement their context ran.
def _fetchone(method: Any) -> Any:  # noqa: ANN401
    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        row = method(self, *args, **kwargs)
        sql.fetched(self.context, sql.size(row))
        return row

    return counted


def _fetchrows(method: Any) -> Any:  # noqa: ANN401
    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        rows = method(self, *args, **kwargs)
        sql.fetched(self.context, len(rows))
        return rows

    return counted


def _fetchiter(method: Any) -> Any:  # noqa: ANN401
    def rows(self: Any) -> Any:  # noqa: ANN401
        # As it is when nothing counts its rows; otherwise counted once, when the iteration ends.
        return method(self) if sql.counting(self.context) is None else _counted(self, method(self))

    return rows


def _counted(result: Any, rows: Any) -> Any:  # noqa: ANN401
    count = 0
    try:
        for row in rows:
            count += 1
            yield row
    finally:
        sql.fetched(result.context, count)


def _after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:  # noqa: ANN401, ARG001, FBT001, PLR0913, PLR0917
    _finish(conn, statement, parameters, context, many=executemany, status="ok")


def _error(exception_context: Any) -> None:  # noqa: ANN401 (sqlalchemy.engine.ExceptionContext)
    context = exception_context.execution_context
    if context is None:
        return
    _finish(
        exception_context.connection,
        exception_context.statement or "",
        exception_context.parameters,
        context,
        many=bool(getattr(context, "executemany", False)),
        status="error",
    )


# Transactions and savepoints open on a connection, innermost last (kept in its info: it outlives a checkout), each
# with its trace (None: the trace had no room left for it).
_OPEN = "tracepanel_transactions"


def _transaction(conn: Any) -> Span | None:  # noqa: ANN401 (sqlalchemy.engine.Connection)
    """The transaction a statement on the connection runs in, when it isn't already the current span or above it.

    A transaction can begin where its span doesn't stay current: FastAPI runs a sync dependency in a worker thread,
    in a copy of the request's context. A statement is in the connection's transaction all the same. A current span
    that began before the transaction can't be inside it; a later one may be (a template the statement runs in).
    """
    info = getattr(conn, "info", None)
    stack: list[tuple[OpenSpan | None, Trace]] = (info or {}).get(_OPEN) or []
    if not stack:
        return None
    opened, trace = stack[-1]
    current = current_span()
    if opened is None or trace is not current_trace() or current is None or current.start >= opened.span.start:
        return None
    return opened.span


def _open_span(conn: Any, *, savepoint: bool) -> None:  # noqa: ANN401 (sqlalchemy.engine.Connection)
    trace = current_trace()
    if trace is None:
        return
    attrs: Attrs = {**_database(conn), "db.transaction.savepoint": savepoint, "code.origin": origin()}
    name = f"{'savepoint' if savepoint else 'transaction'} {engine_alias(conn.engine)}"
    conn.info.setdefault(_OPEN, []).append((open_span("transaction", name, attrs), trace))


def _close_spans(info: dict[str, Any], outcome: str, *, savepoint_only: bool = False) -> None:
    """End the innermost savepoint (savepoint_only), or everything still open on the connection."""
    stack: list[tuple[OpenSpan | None, Trace]] = info.get(_OPEN) or []
    while stack:
        opened, _ = stack.pop()
        if opened is not None:
            opened.span.attrs["db.transaction.outcome"] = outcome
            # In whatever context it ends (another worker thread than the one it began in): its parent is current.
            opened.end()
        if savepoint_only:
            return


def _autocommit(conn: Any) -> bool:  # noqa: ANN401
    """AUTOCOMMIT, on the engine or the connection: SQLAlchemy still begins, but no transaction is held open."""
    check = getattr(conn, "_is_autocommit_isolation", None)
    if callable(check):
        return bool(check())
    return conn.get_execution_options().get("isolation_level") == "AUTOCOMMIT"


def _begin(conn: Any) -> None:  # noqa: ANN401
    if not _autocommit(conn):
        _open_span(conn, savepoint=False)


def _commit(conn: Any) -> None:  # noqa: ANN401
    _close_spans(conn.info, "commit")


def _rollback(conn: Any) -> None:  # noqa: ANN401
    _close_spans(conn.info, "rollback")


def _savepoint(conn: Any, name: str) -> None:  # noqa: ANN401, ARG001 (SQLAlchemy's signature)
    _open_span(conn, savepoint=True)


def _release_savepoint(conn: Any, name: str, context: Any) -> None:  # noqa: ANN401, ARG001
    _close_spans(conn.info, "commit", savepoint_only=True)


def _rollback_savepoint(conn: Any, name: str, context: Any) -> None:  # noqa: ANN401, ARG001
    _close_spans(conn.info, "rollback", savepoint_only=True)


def _checkin(dbapi_connection: Any, record: Any) -> None:  # noqa: ANN401, ARG001
    """Back in the pool with a transaction open (a session closed without commit()): the pool rolls it back."""
    _close_spans(record.info, "rollback")


# ---------- a new connection ----------

# How far up the stack a pool's connect looks for the engine asking: Engine.raw_connection() is a few frames up.
_ENGINE_DEPTH = 16


def _engine_of(pool: Any, frame: FrameType | None) -> Any:  # noqa: ANN401 (a Pool, and its Engine or None)
    """The engine the pool opens a connection for: a pool knows its dialect only, the engine asking is on the stack."""
    from sqlalchemy.engine import Engine  # noqa: PLC0415 (installed: its pool is connecting)

    for _ in range(_ENGINE_DEPTH):
        if frame is None:
            break
        asking = frame.f_locals.get("self")
        if isinstance(asking, Engine) and asking.pool is pool:
            return asking
        frame = frame.f_back
    return None


def _connect(connect: Any) -> Any:  # noqa: ANN401 (_ConnectionRecord.__connect)
    """A pool opening a connection, at checkout: a `sql.connect` span, the driver's and the dialect's work inside.

    The first connection of an engine also initializes its dialect (queries of its own, not recorded as statements)
    and every one runs the app's `connect` listeners: a new connection costs all of it. A SQLite file opens in no
    time.
    """

    @functools.wraps(connect)
    def traced(self: Any) -> None:  # noqa: ANN401 (a _ConnectionRecord)
        if current_trace() is None:
            connect(self)
            return
        pool = self._ConnectionRecord__pool  # the record's pool, name-mangled
        engine = _engine_of(pool, sys._getframe(1))  # noqa: SLF001 (the stack tells who asked)
        vendor = str((engine.dialect if engine is not None else pool._dialect).name)  # noqa: SLF001
        if vendor == "sqlite":
            connect(self)
            return
        attrs = _engine_database(engine) if engine is not None else {"db.client": "sqlalchemy", "db.vendor": vendor}
        with sql.connecting(system(vendor), {**attrs, "db.connection.pool": "sqlalchemy"}):
            connect(self)

    return traced


def install() -> None:
    """Listen on every engine, present and future; a no-op without SQLAlchemy."""
    try:
        from sqlalchemy import event  # noqa: PLC0415 (optional dependency)
        from sqlalchemy.engine import Engine  # noqa: PLC0415
        from sqlalchemy.pool import Pool  # noqa: PLC0415
    except ImportError:
        return
    if getattr(Engine, MARKER, False):
        return
    from sqlalchemy.engine.cursor import CursorResult  # noqa: PLC0415
    from sqlalchemy.pool.base import _ConnectionRecord  # noqa: PLC0415

    # Its private __connect(), name-mangled: where a pool opens every connection, and runs the connect events.
    record_connect = vars(_ConnectionRecord)["_ConnectionRecord__connect"]
    patch(_ConnectionRecord, "_ConnectionRecord__connect", _connect(record_connect))

    for name, counting in (
        ("_fetchone_impl", _fetchone),
        ("_fetchmany_impl", _fetchrows),
        ("_fetchall_impl", _fetchrows),
        ("_fetchiter_impl", _fetchiter),
    ):
        patch(CursorResult, name, counting(getattr(CursorResult, name)))
    event.listen(Engine, "before_cursor_execute", _before)
    event.listen(Engine, "after_cursor_execute", _after)
    event.listen(Engine, "handle_error", _error)
    event.listen(Engine, "begin", _begin)
    event.listen(Engine, "commit", _commit)
    event.listen(Engine, "rollback", _rollback)
    event.listen(Engine, "savepoint", _savepoint)
    event.listen(Engine, "release_savepoint", _release_savepoint)
    event.listen(Engine, "rollback_savepoint", _rollback_savepoint)
    event.listen(Pool, "checkin", _checkin)
    setattr(Engine, MARKER, True)
