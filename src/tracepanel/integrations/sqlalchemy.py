"""SQLAlchemy: every statement of every engine as an `sql` span, every transaction as a `transaction` span.

The second database layer on tracepanel.core.sql (Django's ORM is the first): the statement, its parameters for EXPLAIN,
its place in project code and the N+1 / duplicate analysis are the same. Through SQLAlchemy's own events, so whatever
runs on it is covered (sqlakit, SQLModel, Flask-SQLAlchemy). Turned on when SQLAlchemy is installed.

Several databases: each engine gets its own `db.alias`: its `logging_name` when set (`create_engine(url,
logging_name="replica")`), else the database's name, made unique with the host (or a number) when another engine
already has it. EXPLAIN runs on the engine of that alias.
"""

import time
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tracepanel.core import explain as plans
from tracepanel.core import forks, sql
from tracepanel.core.context import OpenSpan, current_span, current_trace, now_us, open_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, Scalar, Span, Trace
from tracepanel.core.patching import patch
from tracepanel.core.services import database

MARKER = "_tracepanel"
# Where a statement's timing waits between before_cursor_execute and after_cursor_execute.
_STARTED = "_tracepanel_started"


def _database(connection: Any) -> Attrs:  # noqa: ANN401 (sqlalchemy.engine.Connection)
    url = connection.engine.url
    vendor = str(connection.dialect.name)
    return {
        "db.client": "sqlalchemy",
        "db.alias": engine_alias(connection.engine),
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

    def execute(sql_text: str, values: Sequence[Scalar] | Mapping[str, Scalar] | None) -> tuple[plans.Rows, list[str]]:
        connection = engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute(sql_text, values if values is not None else ())
            rows = [tuple(row) for row in cursor.fetchall()]
            return rows, [str(column[0]) for column in cursor.description or []]
        except Exception as exc:
            msg = f"The database could not explain this query: {exc}"
            raise plans.ExplainError(msg) from exc
        finally:
            connection.close()

    return plans.explain(
        statement, params, vendor=str(engine.dialect.name), alias=alias, execute=execute, analyze=analyze
    )


def _before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:  # noqa: ANN401, ARG001, FBT001, PLR0913, PLR0917 (SQLAlchemy's signature)
    if current_trace() is not None and context is not None:
        setattr(context, _STARTED, (now_us(), time.perf_counter_ns()))


def _finish(conn: Any, statement: str, parameters: Any, context: Any, *, many: bool, status: str) -> None:  # noqa: ANN401, PLR0913
    started = getattr(context, _STARTED, None)
    if started is None:
        return
    start, counter = started
    setattr(context, _STARTED, None)
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
