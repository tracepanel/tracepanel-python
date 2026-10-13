"""SQL statements as spans, for any database layer: Django's execute wrappers, SQLAlchemy's events later.

The layer runs the statement through run() and says where it ran (`database`: alias, vendor, system, server);
everything else is here: the span's name, the normalized statement and its fingerprint (what analysis groups by),
the parameters kept for EXPLAIN, the place in project code, the template that issued it.
"""

import datetime
import decimal
import functools
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

from tracepanel.core import conf
from tracepanel.core.context import blocking_attrs, current_span, current_trace, now_us, record_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, Scalar

T = TypeVar("T")
Params = Sequence[object] | Mapping[str, object] | None

_WHITESPACE = re.compile(r"\s+")
_IN_LIST = re.compile(r"\bIN \((?:%s|\?)(?:, ?(?:%s|\?))*\)", re.IGNORECASE)
_TABLE = re.compile(r'\b(?:FROM|INTO|UPDATE|JOIN)\s+[`"\[]?([\w.]+)', re.IGNORECASE)
_COUNT = re.compile(r"^\s*SELECT\s+COUNT\(", re.IGNORECASE)


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
    words = sql.split(None, 1)
    verb = words[0].upper() if words else "SQL"
    if _COUNT.match(sql):
        verb = "SELECT COUNT"
    table = _TABLE.search(sql)
    return f"{verb} {table.group(1)}" if table else verb


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


def run(execute: Callable[[], T], sql: str, params: Params, *, many: bool, database: Callable[[], Attrs]) -> T:
    """Run a statement and record it as an `sql` span of the current trace (outside a trace it only runs).

    `database` describes where it ran (db.alias, db.vendor, db.system, db.namespace, server.*); it is only called
    when there is a trace to record into. For layers that report statements before and after rather than wrapping
    them (SQLAlchemy's events), see record().
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
        record(
            sql,
            params,
            many=many,
            database=database,
            start=start,
            duration=(time.perf_counter_ns() - started) // 1000,
            status=status,
            extra=blocking,
        )


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
) -> None:
    """Record a statement that already ran as an `sql` span of the current trace (no-op outside a trace).

    `extra`: more attributes, such as `code.blocks_event_loop` for a statement run synchronously on an event loop.
    """
    if current_trace() is None:
        return
    parent = current_span()
    template = parent.attrs.get("template.name") if parent is not None and parent.kind == "template" else None
    name, statement, fingerprinted = _parsed(sql)
    record_span(
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
            "code.origin": origin(),
            "code.template": template,
            **({} if many else params_attrs(params)),
            **(extra or {}),
        },
        status=status,
    )
