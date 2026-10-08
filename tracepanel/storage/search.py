"""Search language shared by the local console and the cloud: free text plus `key:value` / `key>=value` filters.

    route:/api/* status>=500 has:n+1 duration>200ms "connection refused"

Parsing is storage-agnostic; `compile_where` turns terms into a parametrized SQLite WHERE clause.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Op = Literal[":", "!=", ">", ">=", "<", "<="]
Param = str | int | float

_TOKEN = re.compile(r'(?:(?P<key>[a-z][a-z0-9_.]*)(?P<op>>=|<=|!=|:|>|<)(?P<value>"[^"]*"|\S+))|(?P<text>"[^"]*"|\S+)')
_DURATION = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>us|µs|ms|s)?$")
_UNITS_US = {"us": 1, "µs": 1, "ms": 1000, "s": 1_000_000, None: 1000}
_AGO = re.compile(r"^(?P<number>\d+)(?P<unit>s|m|h|d)$")
_AGO_US = {"s": 1_000_000, "m": 60_000_000, "h": 3_600_000_000, "d": 86_400_000_000}
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}


class SearchError(ValueError):
    """A query the user can fix: unknown key, bad value or operator."""


@dataclass(frozen=True, slots=True)
class Term:
    key: str | None
    op: Op
    value: str


def parse(query: str) -> list[Term]:
    terms: list[Term] = []
    for match in _TOKEN.finditer(query.strip()):
        if match["text"] is not None:
            terms.append(Term(None, ":", match["text"].strip('"')))
        else:
            op: Op = match["op"]  # type: ignore[assignment]
            terms.append(Term(match["key"], op, match["value"].strip('"')))
    return terms


# ---------- compiling to SQL ----------

Compiler = Callable[[Term], tuple[str, list[Param]]]
_COMPARE = {":": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


def _like(pattern: str) -> str:
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%")


def _values(term: Term) -> list[str]:
    """`a,b,c` is any of the values."""
    return [value for value in term.value.split(",") if value] or [term.value]


def text(column: str) -> Compiler:
    """Exact match, `*` wildcards allowed, `a,b` for any of them; `!=` negates."""

    def compile_term(term: Term) -> tuple[str, list[Param]]:
        if term.op not in {":", "!="}:
            msg = f"{term.key} supports only : and !="
            raise SearchError(msg)
        clauses: list[str] = []
        params: list[Param] = []
        for value in _values(term):
            if "*" in value:
                clauses.append(f"{column} LIKE ? ESCAPE '\\'")
                params.append(_like(value))
            else:
                clauses.append(f"{column} = ?")
                params.append(value)
        clause = " OR ".join(clauses)
        return (f"NOT ({clause})" if term.op == "!=" else clause), params

    return compile_term


def upper_text(column: str) -> Compiler:
    inner = text(column)
    return lambda term: inner(Term(term.key, term.op, term.value.upper()))


def number(column: str, *, parse_value: Callable[[str], float] = float, label: str = "a number") -> Compiler:
    def compile_term(term: Term) -> tuple[str, list[Param]]:
        values = _values(term) if term.op in {":", "!="} else [term.value]
        try:
            parsed: list[Param] = [parse_value(value) for value in values]
        except (ValueError, KeyError):
            msg = f"{term.key} expects {label}, got {term.value!r}"
            raise SearchError(msg) from None
        if len(parsed) > 1:
            marks = ", ".join("?" * len(parsed))
            return f"{column} {'NOT IN' if term.op == '!=' else 'IN'} ({marks})", parsed
        return f"{column} {_COMPARE[term.op]} ?", parsed

    return compile_term


def parse_duration_us(value: str) -> float:
    """`200ms`, `1.5s`, `300us`; a bare number means milliseconds."""
    match = _DURATION.match(value)
    if not match:
        msg = f"not a duration: {value}"
        raise ValueError(msg)
    unit = match["unit"]
    return float(match["number"]) * _UNITS_US[unit]


def duration(column: str) -> Compiler:
    return number(column, parse_value=parse_duration_us, label="a duration like 200ms or 1.5s")


def parse_time_us(value: str, *, now_us: int | None = None) -> int:
    """`15m` / `2h` / `1d` ago, or an ISO date-time; epoch µs.

    Send an offset (`2026-10-07T15:27:14+03:00`): without one the time is read in the server process's zone, which
    Django sets to TIME_ZONE (UTC by default), not the zone of whoever typed it.
    """
    if match := _AGO.match(value):
        now = now_us if now_us is not None else time.time_ns() // 1000
        return now - int(match["number"]) * _AGO_US[match["unit"]]
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return int(moment.timestamp() * 1_000_000)


def time_bound(column: str, *, upper: bool) -> Compiler:
    """`since:` (inclusive) and `until:` (exclusive) bounds on an epoch-µs column."""

    def compile_term(term: Term) -> tuple[str, list[Param]]:
        if term.op != ":":
            msg = f"{term.key} supports only :, like {term.key}:15m"
            raise SearchError(msg)
        try:
            value = parse_time_us(term.value)
        except ValueError:
            msg = f"{term.key} expects a time like 15m, 2h, 1d or 2026-10-07T13:30, got {term.value!r}"
            raise SearchError(msg) from None
        return f"{column} {'<' if upper else '>='} ?", [value]

    return compile_term


def level(column: str) -> Compiler:
    def parse_level(value: str) -> float:
        return LEVELS[value.lower()]

    return number(column, parse_value=parse_level, label=f"a level ({', '.join(LEVELS)})")


# ---------- portable field spec (shared with the cloud; mirrored in schema/search.json) ----------

FieldType = Literal["text", "number", "duration", "level", "has", "time"]


@dataclass(frozen=True, slots=True)
class Field:
    type: FieldType
    help: str


FIELDS: dict[str, dict[str, Field]] = {
    "traces": {
        "kind": Field("text", "http, task, command or websocket"),
        "method": Field("text", "HTTP method: GET, POST…"),
        "route": Field("text", "URL pattern, * wildcards: /api/*"),
        "path": Field("text", "Requested path, * wildcards"),
        "view": Field("text", "View function: blog.views.*"),
        "status": Field("number", "HTTP status: status>=500"),
        "duration": Field("duration", "Total time: duration>200ms"),
        "queries": Field("number", "SQL queries: queries>20"),
        "user": Field("text", "User id"),
        "has": Field("has", "n+1, duplicates, slow-sql, long-transaction, in-transaction, error, issues, logs, mail"),
        "db": Field("text", "Used this database alias: db:replica"),
        "cache": Field("text", "Used this cache alias: cache:sessions"),
        "query": Field("text", "Contains this SQL statement (id from Queries)"),
        "trace": Field("text", "Trace id"),
        "name": Field("text", "Trace name, * wildcards: manage.py *, sync-prices"),
        "operation": Field("text", "GraphQL operation, * wildcards: query PostList"),
        "task": Field("text", "Task name, * wildcards: newsletter.tasks.*"),
        "queue": Field("text", "Queue the task ran from"),
        "state": Field("text", "Task outcome: success, failure, retry"),
        "system": Field("text", "Task queue system: celery, dramatiq, rq, django"),
        "since": Field("time", "Started at or after: since:15m, since:2026-10-07T13:30"),
        "until": Field("time", "Started before: until:5m, until:2026-10-07T14:00"),
    },
    "logs": {
        "level": Field("level", "debug, info, warning, error, critical: level>=warning"),
        "logger": Field("text", "Logger name, * wildcards: blog.*"),
        "trace": Field("text", "Trace id"),
        "route": Field("text", "Route of the request that logged it"),
        "path": Field("text", "Path of the request that logged it"),
        "user": Field("text", "User id of the request that logged it"),
        "extra.*": Field("text", "A field passed in extra=: extra.order_id:42"),
        "since": Field("time", "Logged at or after: since:15m, since:2026-10-07T13:30"),
        "until": Field("time", "Logged before: until:5m, until:2026-10-07T14:00"),
    },
    "queries": {
        "table": Field("text", "Table the statement reads or writes"),
        "db": Field("text", "Database alias"),
        "query": Field("text", "Statement id"),
        "since": Field("time", "Ran at or after: since:15m"),
        "until": Field("time", "Ran before: until:5m"),
    },
    "mails": {
        "to": Field("text", "A recipient (to or cc): to:ann@example.com, * wildcards"),
        "from": Field("text", "Sender address"),
        "since": Field("time", "Sent at or after: since:15m"),
        "until": Field("time", "Sent before: until:5m"),
    },
    "tasks": {
        "task": Field("text", "Task name, * wildcards: newsletter.tasks.*"),
        "queue": Field("text", "Queue the task ran from"),
        "state": Field("text", "success, failure, retry"),
        "system": Field("text", "Which queue ran it: celery, django"),
        "since": Field("time", "Runs at or after: since:15m"),
        "until": Field("time", "Runs before: until:5m"),
    },
    "jobs": {
        "job": Field("text", "Command or tracepanel.trace() name, * wildcards: manage.py *"),
        "source": Field("text", "manage.py or custom (tracepanel.trace)"),
        "since": Field("time", "Runs at or after: since:15m"),
        "until": Field("time", "Runs before: until:5m"),
    },
    "graphql": {
        "operation": Field("text", "Operation, * wildcards: query PostList"),
        "type": Field("text", "query, mutation or subscription"),
        "since": Field("time", "Calls at or after: since:15m"),
        "until": Field("time", "Calls before: until:5m"),
    },
    "websockets": {
        "consumer": Field("text", "Consumer class, * wildcards: chat.consumers.*"),
        "event": Field("text", "connect, receive, disconnect, or a channel layer message type: chat.message"),
        "since": Field("time", "Events at or after: since:15m"),
        "until": Field("time", "Events before: until:5m"),
    },
    "endpoints": {
        "route": Field("text", "URL pattern, * wildcards"),
        "method": Field("text", "HTTP method"),
        "since": Field("time", "Calls at or after: since:15m"),
        "until": Field("time", "Calls before: until:5m"),
    },
    "services": {
        "kind": Field("text", "database, cache, mail, queue, http"),
        "system": Field("text", "postgresql, redis, smtp, rabbitmq…"),
        "role": Field("text", "Who calls it: web, worker, command"),
        "since": Field("time", "Calls at or after: since:15m"),
        "until": Field("time", "Calls before: until:5m"),
    },
    "issues": {
        "kind": Field("text", "error, n+1, duplicates, slow-sql, long-transaction, in-transaction"),
        "route": Field("text", "Route of a request it happened in, * wildcards"),
        "since": Field("time", "Last seen at or after: since:15m"),
        "until": Field("time", "Last seen before: until:5m"),
    },
}


def spec() -> dict[str, dict[str, dict[str, str]]]:
    """FIELDS as plain data: served at /api/search and stored in schema/search.json."""
    return {
        resource: {name: {"type": field.type, "help": field.help} for name, field in fields.items()}
        for resource, fields in FIELDS.items()
    }


@dataclass(frozen=True, slots=True)
class Schema:
    """Searchable fields of one resource and how free text is matched."""

    fields: dict[str, Compiler]
    free_text: Callable[[str], tuple[str, list[Param]]]
    # "extra." -> compiler factory for any key under that prefix (fields spelled "extra.*").
    prefixed: dict[str, Callable[[str], Compiler]] = field(default_factory=dict)


def schema_for(
    resource: str,
    columns: dict[str, str],
    *,
    free_text: Callable[[str], tuple[str, list[Param]]],
    custom: dict[str, Compiler] | None = None,
    prefixed: dict[str, Callable[[str], Compiler]] | None = None,
) -> Schema:
    """Bind a resource's portable fields to storage columns; `custom` covers fields that need more than a column."""
    by_type: dict[str, Callable[[str], Compiler]] = {
        "text": text,
        "number": number,
        "duration": duration,
        "level": level,
    }
    compilers: dict[str, Compiler] = {}
    prefixes: dict[str, Callable[[str], Compiler]] = {}
    for name, declared in FIELDS[resource].items():
        if name.endswith(".*"):
            if not prefixed or name not in prefixed:
                msg = f"{resource}.{name} needs a prefixed compiler"
                raise TypeError(msg)
            prefixes[name[:-1]] = prefixed[name]
        elif custom and name in custom:
            compilers[name] = custom[name]
        elif declared.type == "has" or name not in columns:
            msg = f"{resource}.{name} needs a custom compiler"
            raise TypeError(msg)
        elif declared.type == "time":
            compilers[name] = time_bound(columns[name], upper=name == "until")
        else:
            compilers[name] = by_type[declared.type](columns[name])
    return Schema(fields=compilers, free_text=free_text, prefixed=prefixes)


def compile_where(query: str, schema: Schema) -> tuple[str, list[Param]]:
    clauses: list[str] = []
    params: list[Param] = []
    words: list[str] = []
    for term in parse(query):
        if term.key is None:
            words.append(term.value)
            continue
        compiler = schema.fields.get(term.key)
        if compiler is None:
            prefix = next((p for p in schema.prefixed if term.key.startswith(p) and len(term.key) > len(p)), None)
            compiler = schema.prefixed[prefix](term.key) if prefix else None
        if compiler is None:
            known = ", ".join(sorted(schema.fields))
            msg = f"Unknown filter {term.key!r}. Use one of: {known}"
            raise SearchError(msg)
        clause, values = compiler(term)
        clauses.append(clause)
        params.extend(values)
    if words:
        clause, values = schema.free_text(" ".join(words))
        clauses.append(clause)
        params.extend(values)
    return (" AND ".join(f"({clause})" for clause in clauses) or "1 = 1"), params
