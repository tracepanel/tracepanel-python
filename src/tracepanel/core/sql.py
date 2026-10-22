"""SQL statements as spans, for any database layer: Django's execute wrappers, SQLAlchemy's events later.

The layer runs the statement through run() and says where it ran (`database`: alias, vendor, system, server);
everything else is here: the span's name, the normalized statement and its fingerprint (what analysis groups by),
the parameters kept for EXPLAIN, the place in project code, the template that issued it and its line.
"""

import contextlib
import datetime
import decimal
import functools
import re
import time
import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

from tracepanel.core import conf
from tracepanel.core.context import blocking_attrs, current_span, current_trace, now_us, record_span
from tracepanel.core.frames import library_caller, origin, template_place
from tracepanel.core.model import Attrs, Scalar, Span

T = TypeVar("T")
Params = Sequence[object] | Mapping[str, object] | None

_WHITESPACE = re.compile(r"\s+")
# A placeholder, as each driver writes it: %s and %(name)s (Django, psycopg, PyMySQL, SQLAlchemy on pymssql or
# psycopg2), ? and ?1 (sqlite3, pyodbc, JDBC), $1 (PostgreSQL), @P1 and @name (SQL Server), :1 and :name (Oracle,
# SQLAlchemy). The agent's rules, so a statement recorded here and one it maps from OpenTelemetry group alike.
_PLACEHOLDER = r"(?:%s|%\([A-Za-z0-9_]+\)s|\?[0-9]*|\$[0-9]+|@[A-Za-z0-9_]+|:[A-Za-z0-9_]+)"
_IN_LIST = re.compile(rf"\bIN \({_PLACEHOLDER}(?:, ?{_PLACEHOLDER})*\)", re.IGNORECASE)
# The table a statement reads or writes, its schema too, each part bare or quoted: "standard SQL", `MySQL`,
# [SQL Server] (`[dbo].[orders]`). The first part and the rest are apart, so a name of one part, the usual one, needs
# no quotes dropped. The lookahead lets the search skip a word that can't start a keyword.
_TABLE = re.compile(
    r'\b(?=[FIJUfiju])(?:FROM|INTO|UPDATE|JOIN)\s+[`"\[]?(\w+)[`"\]]?((?:\.[`"\[]?\w+[`"\]]?)*)', re.IGNORECASE
)
_QUOTES = str.maketrans("", "", '`"[]')
_COUNT = re.compile(r"^\s*SELECT\s+COUNT\(", re.IGNORECASE)


def _origin() -> dict[str, str | None]:
    """Where the project's code made the query; for a query only library code made, which library code (code.caller)."""
    made = origin(3)
    return {"code.origin": made} if made is not None else {"code.origin": None, "code.caller": library_caller(3)}


def _template(parent: Span | None) -> Attrs:
    """The template a query ran in, as it rendered (code.template), and its line there when the engine says it.

    The template is the file of the tag or variable that made the query: a page that extends another names its own
    file for a query in one of its blocks, the base template's for one in the rest.
    """
    if parent is None or parent.kind != "template":
        return {"code.template": None}
    name = parent.attrs.get("template.name")
    place = template_place(3)
    if place is None:
        return {"code.template": name}
    return {"code.template": place[0] or name, "code.template_line": place[1]}


def fingerprint(sql: str) -> str:
    """Normalize a parametrized query so the same statement maps to one fingerprint.

    It is the span's `db.fingerprint`: what the agent groups queries, and finds N+1 and duplicates, by.
    """
    sql = _WHITESPACE.sub(" ", sql).strip()
    return _IN_LIST.sub("IN (...)", sql)


@functools.lru_cache(maxsize=2048)
def _parsed(sql: str) -> tuple[str, str, str]:
    """(span name, one-line statement, fingerprint) of a statement; templates repeat, so they are parsed once."""
    statement = " ".join(sql.split())
    return span_name(sql), statement, fingerprint(sql)


def span_name(sql: str) -> str:
    """A statement's span name: its verb and its table, schema kept, quotes dropped (`SELECT dbo.orders`)."""
    words = sql.split(None, 1)
    verb = words[0].upper() if words else "SQL"
    if verb == "SELECT" and _COUNT.match(sql):
        verb = "SELECT COUNT"
    table = _TABLE.search(sql)
    if table is None:
        return verb
    name, schema_and_rest = table.groups()
    return f"{verb} {name}{schema_and_rest.translate(_QUOTES)}" if schema_and_rest else f"{verb} {name}"


def params_hash(params: Params) -> str:
    return format(hash(repr(params)) & 0xFFFFFFFFFFFF, "012x")


MAX_PARAMS = 200
MAX_PARAM_CHARS = 2000
_AS_TEXT = (datetime.date, datetime.time, datetime.timedelta, decimal.Decimal, uuid.UUID)


class _UnexplainableError(ValueError):
    pass


def _value(value: object) -> Scalar:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, _AS_TEXT):
        return str(value)
    if isinstance(value, str) and len(value) <= MAX_PARAM_CHARS:
        return value
    # Bytes, huge strings, arrays, JSON objects: kept out, so EXPLAIN never runs with altered values.
    raise _UnexplainableError


def params_attrs(params: Params) -> Attrs:
    """Parameter values for EXPLAIN in the console: kept locally only, and only when every value round-trips.

    Positional parameters become `db.params`; named ones also get `db.param_names`.
    """
    if params is None or not conf.get_config().capture_sql_params:
        return {}
    try:
        if isinstance(params, Mapping):
            if len(params) > MAX_PARAMS:
                return {}
            return {
                "db.param_names": [str(name) for name in params],
                "db.params": [_value(value) for value in params.values()],
            }
        if isinstance(params, (str, bytes)) or len(params) > MAX_PARAMS:
            return {}
        return {"db.params": [_value(value) for value in params]}
    except _UnexplainableError:
        return {}


# How many rows a SELECT returned, OpenTelemetry's name: the agent finds a SELECT without a LIMIT that read many.
RETURNED_ROWS = "db.response.returned_rows"


# Where a statement's span waits for its rows: on the cursor (or result) they are fetched from, or for one that takes
# no attributes (psycopg's cursors have __slots__), by a weak reference to it.
_ROWS_SPAN = "_tracepanel_rows_span"
_MISSING = object()
_rows_spans: weakref.WeakKeyDictionary[object, Span] = weakref.WeakKeyDictionary()


def count_rows(span: Span, rows: int) -> None:
    """Add rows a statement returned (fetched now, or known from the driver) to its span."""
    known = span.attrs.get(RETURNED_ROWS)
    span.attrs[RETURNED_ROWS] = (known if isinstance(known, int) else 0) + rows


def counts_into(owner: object, span: Span | None) -> None:
    """The rows fetched from `owner` from now on count into `span`, or nowhere."""
    try:
        setattr(owner, _ROWS_SPAN, span)
    except (AttributeError, TypeError):
        # Neither attributes nor weak references: its rows aren't counted.
        with contextlib.suppress(TypeError):
            if span is None:
                _rows_spans.pop(owner, None)
            else:
                _rows_spans[owner] = span


def counting(owner: object) -> Span | None:
    """The span the rows fetched from `owner` count into, if any."""
    span = getattr(owner, _ROWS_SPAN, _MISSING)
    if span is _MISSING and _rows_spans:
        with contextlib.suppress(TypeError):
            span = _rows_spans.get(owner)
    return span if isinstance(span, Span) else None


def fetched(owner: object, rows: int) -> None:
    """Rows fetched from `owner`, counted into the span of the statement it last ran, if one is recorded."""
    span = counting(owner)
    if span is not None:
        count_rows(span, rows)


def size(result: object) -> int:
    """How many rows a fetch returned: a list's length, one for a row, none for None."""
    if result is None:
        return 0
    return len(result) if isinstance(result, list) else 1


def run(  # noqa: PLR0913
    execute: Callable[[], T],
    sql: str,
    params: Params,
    *,
    many: bool,
    database: Callable[[], Attrs],
    then: Callable[[Span], None] | None = None,
) -> T:
    """Run a statement and record it as an `sql` span of the current trace (outside a trace it only runs).

    `database` describes where it ran (db.alias, db.vendor, db.system, db.namespace, server.*); it is only called
    when there is a trace to record into. `then` gets the span once recorded, for what is known only afterwards (the
    rows fetched). For layers that report statements before and after rather than wrapping them (SQLAlchemy's
    events), see record().
    """
    if current_trace() is None:
        return execute()
    start = now_us()
    started = time.perf_counter_ns()
    status = "ok"
    blocking = blocking_attrs()
    try:
        return execute()
    except Exception:
        status = "error"
        raise
    finally:
        span = record(
            sql,
            params,
            many=many,
            database=database,
            start=start,
            duration=(time.perf_counter_ns() - started) // 1000,
            status=status,
            extra=blocking,
        )
        if span is not None and then is not None and status == "ok":
            then(span)


def record(  # noqa: PLR0913 (the statement, where it ran, when, how it went)
    sql: str,
    params: Params,
    *,
    many: bool,
    database: Callable[[], Attrs],
    start: int,
    duration: int,
    status: str = "ok",
    extra: Attrs | None = None,
    parent: Span | None = None,
) -> Span | None:
    """Record a statement that already ran as an `sql` span of the current trace (no-op outside a trace).

    `extra`: more attributes, such as `code.blocks_event_loop` for a statement run synchronously on an event loop.
    `parent`: its span when not the current one (the transaction open on its connection).
    """
    if current_trace() is None:
        return None
    parent = parent or current_span()
    name, statement, fingerprinted = _parsed(sql)
    return record_span(
        "sql",
        name,
        start=start,
        duration=duration,
        attrs={
            **database(),
            "db.statement": statement,
            "db.fingerprint": fingerprinted,
            "db.params_hash": params_hash(params),
            "db.many": many,
            **_origin(),
            **_template(parent),
            **({} if many else params_attrs(params)),
            **(extra or {}),
        },
        status=status,
        parent=parent,
    )
