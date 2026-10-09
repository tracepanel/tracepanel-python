"""Local SQLite store in .tracepanel/, shared by every process of the project (web, Celery worker, beat).

Traces are kept as JSON documents plus columns for filtering; logs (with FTS5 full-text search) and SQL calls
are copied into their own tables so the console can search and aggregate across traces.
"""

import hashlib
import json
import os
import sqlite3
import statistics
import threading
import time
from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, TypedDict

from tracepanel.core import conf, services
from tracepanel.core.model import Trace
from tracepanel.storage import search
from tracepanel.storage.search import Param, Term

STORE_VERSION = "13"
TABLES = (
    "meta",
    "traces",
    "issues",
    "issue_traces",
    "logs",
    "logs_fts",
    "sql",
    "mails",
    "links",
    "peers",
    "task_enqueues",
    "task_runs",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    start INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    method TEXT,
    path TEXT,
    route TEXT,
    view TEXT,
    user_id TEXT,
    queries INTEGER NOT NULL,
    db_time INTEGER NOT NULL,
    issues INTEGER NOT NULL,
    issue_kinds TEXT NOT NULL,
    errors INTEGER NOT NULL,
    logs INTEGER NOT NULL,
    mails INTEGER NOT NULL,
    db_aliases TEXT NOT NULL,
    cache_aliases TEXT NOT NULL,
    task_state TEXT,
    htmx TEXT,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_start ON traces (start DESC);
CREATE INDEX IF NOT EXISTS traces_route ON traces (route);
CREATE TABLE IF NOT EXISTS issues (
    fingerprint TEXT PRIMARY KEY,
    fingerprint_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    culprit TEXT,
    hint TEXT,
    occurrences INTEGER NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    last_trace_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_traces (
    fingerprint TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    PRIMARY KEY (fingerprint, trace_id)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    trace_id TEXT NOT NULL,
    time INTEGER NOT NULL,
    level TEXT NOT NULL,
    level_no INTEGER NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    exception TEXT,
    attrs TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_time ON logs (time DESC);
CREATE INDEX IF NOT EXISTS logs_trace ON logs (trace_id);
CREATE TABLE IF NOT EXISTS sql (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    fp TEXT NOT NULL,
    alias TEXT NOT NULL,
    vendor TEXT,
    statement TEXT NOT NULL,
    name TEXT NOT NULL,
    start INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    origin TEXT
);
CREATE INDEX IF NOT EXISTS sql_fp ON sql (fp);
CREATE TABLE IF NOT EXISTS mails (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    time INTEGER NOT NULL,
    subject TEXT NOT NULL,
    sender TEXT,
    recipients TEXT NOT NULL,
    sent INTEGER,
    html INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mails_time ON mails (time DESC);
CREATE TABLE IF NOT EXISTS links (
    trace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    linked_trace TEXT NOT NULL,
    linked_span TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_linked ON links (linked_trace);
CREATE INDEX IF NOT EXISTS links_trace ON links (trace_id);
CREATE INDEX IF NOT EXISTS sql_trace ON sql (trace_id);
CREATE TABLE IF NOT EXISTS peers (
    trace_id TEXT NOT NULL,
    start INTEGER NOT NULL,
    service TEXT NOT NULL,
    role TEXT NOT NULL,
    peer TEXT NOT NULL,
    kind TEXT NOT NULL,
    system TEXT NOT NULL,
    address TEXT,
    port INTEGER,
    namespace TEXT,
    alias TEXT,
    direction TEXT NOT NULL,
    calls INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    errors INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS peers_trace ON peers (trace_id);
CREATE INDEX IF NOT EXISTS peers_start ON peers (start DESC);
-- Task runs whose queue carries no headers (Django Tasks) are linked to their enqueue by task id.
CREATE TABLE IF NOT EXISTS task_enqueues (task_id TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS task_enqueues_task ON task_enqueues (task_id);
CREATE TABLE IF NOT EXISTS task_runs (task_id TEXT NOT NULL, trace_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS task_runs_task ON task_runs (task_id);
"""
FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(message, logger)"

LISTING = (
    "id, kind, name, start, duration, status, http_status, method, path, route, view, user_id, "
    "queries, db_time, issues, issue_kinds, errors, logs, mails, db_aliases, cache_aliases, task_state, htmx"
)


def query_fingerprint(statement: str, alias: str = "default") -> str:
    """Short stable id of a normalized SQL statement on one database, used in `query:` filters and URLs."""
    return hashlib.sha1(f"{alias}|{statement}".encode(), usedforsecurity=False).hexdigest()[:16]


def _recipients(attrs: Mapping[str, object]) -> str:
    """To and cc as ",a@x,b@y," so `to:` matches whole addresses with LIKE."""
    addresses = {str(address) for key in ("mail.to", "mail.cc") for address in _list(attrs.get(key))}
    return _alias_list(addresses)


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _alias_list(aliases: set[str]) -> str:
    return f",{','.join(sorted(aliases))}," if aliases else ""


def _has(term: Term) -> tuple[str, list[Param]]:
    if term.op != ":":
        msg = "has supports only ':'"
        raise search.SearchError(msg)
    if term.value == "error":
        return "errors > 0 OR issue_kinds LIKE '%,error,%'", []
    if term.value == "logs":
        return "logs > 0", []
    if term.value == "issues":
        return "issues > 0", []
    if term.value == "mail":
        return "mails > 0", []
    if term.value in {"n+1", "duplicates", "slow-sql", "long-transaction", "in-transaction", "htmx"}:
        return "issue_kinds LIKE ?", [f"%,{term.value},%"]
    msg = (
        "has expects one of: n+1, duplicates, slow-sql, long-transaction, in-transaction, htmx, error, issues, logs, "
        "mail"
    )
    raise search.SearchError(msg)


def _trace_text(words: str) -> tuple[str, list[Param]]:
    pattern = f"%{search._like(words)}%"  # noqa: SLF001
    return "name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'", [pattern, pattern]


# Linked traces listed on a trace page (events of one WebSocket connection can be many).
RELATED_LIMIT = 100
WS_LIFECYCLE = frozenset({"connect", "receive", "disconnect"})
OPERATION = '"graphql.operation"'
COMMAND = '"command.name"'
OPERATION_TYPE = '"graphql.operation.type"'
OPERATION_NAME = '"graphql.operation.name"'
SYSTEM = '"task.system"'
QUEUE = '"task.queue"'
STATE = '"task.state"'
WAIT = '"task.queue_wait"'


def _task_name(term: Term) -> tuple[str, list[Param]]:
    clause, params = search.text("name")(term)
    return f"kind = 'task' AND ({clause})", params


TRACE_SEARCH = search.schema_for(
    "traces",
    {
        "kind": "kind",
        "route": "route",
        "path": "path",
        "view": "view",
        "status": "http_status",
        "duration": "duration",
        "queries": "queries",
        "user": "user_id",
        "trace": "id",
        "name": "name",
        "since": "start",
        "until": "start",
    },
    free_text=_trace_text,
    custom={
        "method": search.upper_text("method"),
        "has": _has,
        "query": lambda term: ("id IN (SELECT trace_id FROM sql WHERE fp = ?)", [term.value]),
        "db": lambda term: ("db_aliases LIKE ?", [f"%,{term.value},%"]),
        "task": _task_name,
        "operation": search.text(f"json_extract(doc, '$.attrs.{OPERATION}')"),
        "queue": search.text(f"json_extract(doc, '$.attrs.{QUEUE}')"),
        "state": search.upper_text(f"json_extract(doc, '$.attrs.{STATE}')"),
        "system": search.text(f"json_extract(doc, '$.attrs.{SYSTEM}')"),
        "cache": lambda term: ("cache_aliases LIKE ?", [f"%,{term.value},%"]),
    },
)


SLOW_REQUEST_US = 500_000
"""Requests at least this slow count as slow in the overview."""


def _resolver_stats(docs: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Per GraphQL field over many calls: how often it resolves per call, its time, its SQL; heaviest first."""
    fields: dict[str, dict[str, Any]] = {}
    for doc in docs:
        spans = doc["spans"]
        parents = {span["id"]: span for span in spans}
        for span in spans:
            field = span["attrs"].get("graphql.field")
            if span["kind"] != "function" or not isinstance(field, str):
                continue
            item = fields.setdefault(field, {"field": field, "resolves": 0, "durations": [], "queries": 0})
            item["resolves"] += 1
            item["durations"].append(int(span["duration"]))
        for span in spans:
            if span["kind"] != "sql":
                continue
            parent = parents.get(span.get("parent_id") or "")
            field = parent["attrs"].get("graphql.field") if parent else None
            if isinstance(field, str) and field in fields:
                fields[field]["queries"] += 1
    calls = max(len(docs), 1)
    rows = [
        {
            "field": item["field"],
            "per_call": round(item["resolves"] / calls, 1),
            "total": sum(item["durations"]),
            "p95": _pct(sorted(item["durations"]), 0.95),
            "queries_per_call": round(item["queries"] / calls, 1),
        }
        for item in fields.values()
    ]
    return sorted(rows, key=lambda row: -int(str(row["total"])))


def _pct(values: list[int], p: float) -> int:
    """Nearest-rank percentile of sorted values."""
    return values[min(len(values) - 1, int(len(values) * p))] if values else 0


def _is_error(row: sqlite3.Row) -> bool:
    """Failed work: the trace ended in error (an unhandled exception, a failed task) or answered 5xx.

    A handled exception inside (logged, retried) is not a failure of the request or task itself.
    """
    return row["status"] == "error" or (row["http_status"] or 0) >= 500  # noqa: PLR2004


def _group_stats(rows: list[sqlite3.Row]) -> dict[str, int]:
    return {
        "calls": len(rows),
        "errors": sum(1 for row in rows if _is_error(row)),
        "p95": _pct(sorted(int(row["duration"]) for row in rows), 0.95),
        "slow": sum(1 for row in rows if int(row["duration"]) >= SLOW_REQUEST_US),
    }


def _stats(
    rows: list[sqlite3.Row], buckets: int, *, start: int | None = None, end: int | None = None
) -> dict[str, object]:
    """Summary, time series and latency histogram of traces ordered by start; the series spans [start, end]."""
    if start is None and rows:
        start = int(rows[0]["start"])
    if end is None and rows:
        end = int(rows[-1]["start"])
    if start is None or end is None or end < start:
        return {"summary": None, "series": [], "histogram": []}
    durations = sorted(int(row["duration"]) for row in rows)
    width = max((end - start) // buckets + 1, 1)
    series: list[dict[str, object]] = []
    for index in range(buckets):
        low = start + index * width
        bucket = [row for row in rows if low <= int(row["start"]) < low + width]
        values = sorted(int(row["duration"]) for row in bucket)
        series.append(
            {
                "start": low,
                "end": low + width,
                "calls": len(bucket),
                "errors": sum(1 for row in bucket if _is_error(row)),
                "slow": sum(1 for value in values if value >= SLOW_REQUEST_US),
                "p50": _pct(values, 0.5) if values else None,
                "p95": _pct(values, 0.95) if values else None,
            }
        )
    # Log-spaced latency buckets from 100 µs up, doubling per bucket.
    edges = [100 * 2**i for i in range(22)]
    histogram = [
        {"from": low, "to": high, "count": sum(1 for d in durations if low <= d < high)}
        for low, high in zip([0, *edges[:-1]], edges, strict=True)
    ]
    while histogram and histogram[-1]["count"] == 0:
        histogram.pop()
    while len(histogram) > 1 and histogram[0]["count"] == 0 and histogram[1]["count"] == 0:
        histogram.pop(0)
    if not rows:
        return {"summary": None, "series": series, "histogram": histogram}
    return {
        "summary": {
            "calls": len(rows),
            "errors": sum(1 for row in rows if _is_error(row)),
            "slow": sum(1 for value in durations if value >= SLOW_REQUEST_US),
            "p50": _pct(durations, 0.5),
            "p75": _pct(durations, 0.75),
            "p90": _pct(durations, 0.9),
            "p95": _pct(durations, 0.95),
            "p99": _pct(durations, 0.99),
            "max": durations[-1],
            "avg_queries": round(statistics.fmean(int(row["queries"]) for row in rows), 1),
            "avg_db_time": round(statistics.fmean(int(row["db_time"]) for row in rows)),
            "with_issues": sum(1 for row in rows if row["issues"]),
            "first_seen": int(rows[0]["start"]),
            "last_seen": int(rows[-1]["start"]),
        },
        "series": series,
        "histogram": histogram,
    }


class Related(TypedDict):
    """Traces linked to one trace, by link kind (see `related`)."""

    enqueued_by: dict[str, object] | None
    enqueued: list[dict[str, object]]
    connection: dict[str, object] | None
    connection_events: list[dict[str, object]]
    sent_by: dict[str, object] | None
    delivered: list[dict[str, object]]
    # Linked trace id -> the span of this trace it came from (an enqueue, a channel layer send).
    via: dict[str, str]


class Store:
    def __init__(self, path: Path, max_traces: int = 2000) -> None:
        self.path = path
        self.max_traces = max_traces
        # One connection per writing thread (the background writer), reopened after a fork.
        self._writing = threading.local()
        self._written_since_prune = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            version = None
            if db.execute("SELECT name FROM sqlite_master WHERE name = 'meta'").fetchone():
                row = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
                version = row[0] if row else None
            if version != STORE_VERSION:
                # Local data is disposable: recreate on a store format change.
                for table in TABLES:
                    db.execute(f"DROP TABLE IF EXISTS {table}")
            db.executescript(SCHEMA)
            try:
                db.execute(FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                # SQLite built without FTS5: log search falls back to LIKE.
                self.fts = False
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (STORE_VERSION,))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    # ---------- writing ----------

    def _write_connection(self) -> sqlite3.Connection:
        local = self._writing
        if getattr(local, "pid", None) != os.getpid():
            local.db, local.pid = self._connect(), os.getpid()
        db: sqlite3.Connection = local.db
        return db

    def write(self, traces: list[Trace]) -> None:
        db = self._write_connection()
        with db:
            for trace in traces:
                self._write_trace(db, trace)
            self._written_since_prune += len(traces)
            # Rotation scans every table: do it once the store is 10% over its limit, not on every write.
            if self._written_since_prune >= max(self.max_traces // 10, 1):
                self._prune(db)
                self._written_since_prune = 0

    def _prune(self, db: sqlite3.Connection) -> None:
        """Keep the newest `max_traces` traces, and drop the rows of the removed ones."""
        db.execute(
            "DELETE FROM traces WHERE id NOT IN (SELECT id FROM traces ORDER BY start DESC LIMIT ?)",
            (self.max_traces,),
        )
        orphan = "trace_id NOT IN (SELECT id FROM traces)"
        if self.fts:
            db.execute(f"DELETE FROM logs_fts WHERE rowid IN (SELECT id FROM logs WHERE {orphan})")  # noqa: S608
        for table in ("logs", "sql", "issue_traces", "mails", "links", "peers", "task_enqueues", "task_runs"):
            db.execute(f"DELETE FROM {table} WHERE {orphan}")  # noqa: S608 (constant table names)

    def clear(self) -> int:
        """Delete every stored trace and what came with it (logs, queries, emails, issues); returns how many traces."""
        with closing(self._connect()) as db, db:
            (count,) = db.execute("SELECT COUNT(*) FROM traces").fetchone()
            for table in TABLES:
                if table == "meta" or (table == "logs_fts" and not self.fts):
                    continue
                db.execute(f"DELETE FROM {table}")  # noqa: S608 (constant table names)
        return int(count)

    def _write_trace(self, db: sqlite3.Connection, trace: Trace) -> None:
        attrs = trace.attrs
        sql_spans = [span for span in trace.spans if span.kind == "sql"]
        kinds = sorted({issue.kind for issue in trace.issues})
        db_aliases = {str(span.attrs.get("db.alias") or "default") for span in sql_spans}
        cache_spans = [span for span in trace.spans if span.kind == "cache"]
        cache_aliases = {str(span.attrs.get("cache.alias") or "default") for span in cache_spans}
        db.execute(
            f"INSERT OR REPLACE INTO traces ({LISTING}, doc) VALUES ({', '.join('?' * 24)})",  # noqa: S608
            (
                trace.id,
                trace.kind,
                trace.name,
                trace.start,
                trace.duration,
                trace.status,
                attrs.get("http.status_code"),
                attrs.get("http.method"),
                attrs.get("http.path"),
                attrs.get("http.route"),
                attrs.get("code.view"),
                attrs.get("user.id"),
                len(sql_spans),
                sum(span.duration for span in sql_spans),
                # Findings only: an error is counted in `errors` (and kept with the issues for grouping).
                sum(1 for issue in trace.issues if issue.kind != "error"),
                f",{','.join(kinds)}," if kinds else "",
                len(trace.errors),
                len(trace.logs),
                sum(1 for span in trace.spans if span.kind == "mail"),
                _alias_list(db_aliases),
                _alias_list(cache_aliases),
                str(attrs["task.state"]).lower() if attrs.get("task.state") else None,
                ("boosted" if attrs.get("htmx.boosted") else "request") if attrs.get("htmx.request") else None,
                json.dumps(trace.to_dict(), separators=(",", ":")),
            ),
        )
        for log in trace.logs:
            cursor = db.execute(
                "INSERT INTO logs (trace_id, time, level, level_no, logger, message, exception, attrs)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace.id,
                    log.time,
                    log.level,
                    search.LEVELS.get(log.level, 0),
                    log.logger,
                    log.message,
                    log.exception,
                    json.dumps(log.attrs),
                ),
            )
            if self.fts:
                db.execute(
                    "INSERT INTO logs_fts (rowid, message, logger) VALUES (?, ?, ?)",
                    (cursor.lastrowid, log.message, log.logger),
                )
        db.executemany(
            "INSERT INTO sql (trace_id, span_id, fp, alias, vendor, statement, name, start, duration, origin)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    trace.id,
                    span.id,
                    query_fingerprint(
                        str(span.attrs.get("db.fingerprint", span.name)), str(span.attrs.get("db.alias") or "default")
                    ),
                    str(span.attrs.get("db.alias") or "default"),
                    span.attrs.get("db.system") or span.attrs.get("db.vendor"),
                    str(span.attrs.get("db.fingerprint", span.name)),
                    span.name,
                    span.start,
                    span.duration,
                    span.attrs.get("code.origin"),
                )
                for span in sql_spans
            ],
        )
        service, role = str(attrs.get("service.name") or ""), str(attrs.get("service.role") or trace.kind)
        db.executemany(
            "INSERT INTO peers (trace_id, start, service, role, peer, kind, system, address, port, namespace, alias,"
            " direction, calls, duration, errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    trace.id,
                    trace.start,
                    service,
                    role,
                    peer.id,
                    peer.kind,
                    peer.system,
                    peer.address,
                    peer.port,
                    peer.namespace,
                    peer.alias,
                    direction,
                    usage.calls,
                    usage.duration,
                    usage.errors,
                )
                for (peer, direction), usage in services.dependencies(trace).items()
            ],
        )
        db.executemany(
            "INSERT INTO links (trace_id, kind, linked_trace, linked_span) VALUES (?, ?, ?, ?)",
            [(trace.id, link.kind, link.trace_id, link.span_id) for link in trace.links],
        )
        self._link_tasks(db, trace)
        db.executemany(
            "INSERT INTO mails (trace_id, span_id, time, subject, sender, recipients, sent, html)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    trace.id,
                    span.id,
                    span.start,
                    str(span.attrs.get("mail.subject") or ""),
                    span.attrs.get("mail.from"),
                    _recipients(span.attrs),
                    span.attrs.get("mail.sent"),
                    span.attrs.get("mail.html") is not None,
                )
                for span in trace.spans
                if span.kind == "mail"
            ],
        )
        for issue in trace.issues:
            db.execute(
                """
                INSERT INTO issues (fingerprint, fingerprint_version, kind, title, culprit, hint,
                                    occurrences, first_seen, last_seen, last_trace_id)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT (fingerprint) DO UPDATE SET
                    title = excluded.title, culprit = excluded.culprit, hint = excluded.hint,
                    occurrences = occurrences + 1, last_seen = excluded.last_seen,
                    last_trace_id = excluded.last_trace_id
                """,
                (
                    issue.fingerprint,
                    issue.fingerprint_version,
                    issue.kind,
                    issue.title,
                    issue.culprit,
                    issue.hint,
                    trace.start,
                    trace.start,
                    trace.id,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO issue_traces (fingerprint, trace_id) VALUES (?, ?)",
                (issue.fingerprint, trace.id),
            )

    @staticmethod
    def _link_tasks(db: sqlite3.Connection, trace: Trace) -> None:
        """Link task runs to their enqueue span by task id, whichever of the two traces is written first."""
        db.executemany(
            "INSERT INTO task_enqueues (task_id, trace_id, span_id) VALUES (?, ?, ?)",
            [
                (str(span.attrs["task.id"]), trace.id, span.id)
                for span in trace.spans
                if span.kind == "task.enqueue" and span.attrs.get("task.id")
            ],
        )
        task_id = trace.attrs.get("task.id")
        if trace.kind == "task" and task_id and not any(link.kind == "enqueued_by" for link in trace.links):
            db.execute("INSERT INTO task_runs (task_id, trace_id) VALUES (?, ?)", (str(task_id), trace.id))
        db.execute(
            """
            INSERT INTO links (trace_id, kind, linked_trace, linked_span)
            SELECT run.trace_id, 'enqueued_by', enqueue.trace_id, enqueue.span_id
            FROM task_runs run JOIN task_enqueues enqueue ON enqueue.task_id = run.task_id
            WHERE (run.trace_id = ? OR enqueue.trace_id = ?)
              AND NOT EXISTS (SELECT 1 FROM links WHERE trace_id = run.trace_id AND kind = 'enqueued_by')
            """,
            (trace.id, trace.id),
        )

    # ---------- reading ----------

    @staticmethod
    def _trace_row(row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        for column in ("issue_kinds", "db_aliases", "cache_aliases"):
            raw = str(item.pop(column) or "")
            values: list[str] = [value for value in raw.split(",") if value]
            item[column] = values
        return item

    def list_traces(self, *, limit: int = 100, ids: list[str] | None = None) -> list[dict[str, object]]:
        """Traces by id (the panel's tab history) or the latest ones."""
        where, params = "1 = 1", list[Param]()
        if ids:
            where, params = f"id IN ({', '.join('?' * len(ids))})", list[Param](ids)
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {LISTING} FROM traces WHERE {where} ORDER BY start DESC LIMIT ?",  # noqa: S608
                [*params, limit],
            )
            return [self._trace_row(row) for row in rows]

    def search_traces(
        self, query: str = "", *, limit: int = 100, before: int | None = None
    ) -> tuple[list[dict[str, object]], int | None]:
        """Newest first; `before` is the `start` of the last row of the previous page."""
        where, params = search.compile_where(query, TRACE_SEARCH)
        if before is not None:
            where += " AND start < ?"
            params.append(before)
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {LISTING} FROM traces WHERE {where} ORDER BY start DESC LIMIT ?",  # noqa: S608
                [*params, limit + 1],
            ).fetchall()
        items = [self._trace_row(row) for row in rows[:limit]]
        cursor = items[-1]["start"] if len(rows) > limit else None
        return items, cursor if isinstance(cursor, int) else None

    def get_trace(self, trace_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT doc FROM traces WHERE id = ?", (trace_id,)).fetchone()
        if row is None:
            return None
        doc: dict[str, object] = json.loads(row["doc"])
        return doc

    def search_logs(
        self, query: str = "", *, limit: int = 100, before: int | None = None
    ) -> tuple[list[dict[str, object]], int | None]:
        schema = search.schema_for(
            "logs",
            {
                "level": "logs.level_no",
                "logger": "logs.logger",
                "trace": "logs.trace_id",
                "route": "traces.route",
                "path": "traces.path",
                "user": "traces.user_id",
                "since": "logs.time",
                "until": "logs.time",
            },
            free_text=self._log_text,
            # Keys come from the query grammar ([a-z0-9_.] only), so they are safe inside the JSON path.
            prefixed={"extra.*": lambda key: search.text(f"CAST(json_extract(logs.attrs, '$.\"{key}\"') AS TEXT)")},
        )
        where, params = search.compile_where(query, schema)
        if before is not None:
            where += " AND logs.time < ?"
            params.append(before)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT logs.id, logs.trace_id, logs.time, logs.level, logs.logger, logs.message, logs.exception,"  # noqa: S608 (constant SQL; values are bound)
                " logs.attrs, traces.kind AS trace_kind, traces.htmx AS trace_htmx, traces.name AS trace_name,"
                " traces.method, traces.path, traces.http_status, traces.user_id"
                f" FROM logs JOIN traces ON traces.id = logs.trace_id WHERE {where}"
                " ORDER BY logs.time DESC, logs.id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        items = [dict(row) | {"attrs": json.loads(row["attrs"])} for row in rows[:limit]]
        cursor = items[-1]["time"] if len(rows) > limit else None
        return items, cursor if isinstance(cursor, int) else None

    def _log_text(self, words: str) -> tuple[str, list[Param]]:
        if self.fts:
            # Each word as a quoted prefix token: safe against FTS syntax, matches as you type.
            tokens = " ".join(f'"{word.replace(chr(34), "")}"*' for word in words.split() if word.strip('"'))
            return "logs.id IN (SELECT rowid FROM logs_fts WHERE logs_fts MATCH ?)", [tokens]
        pattern = f"%{search._like(words)}%"  # noqa: SLF001
        return "logs.message LIKE ? ESCAPE '\\'", [pattern]

    def list_queries(self, query: str = "", *, limit: int = 100) -> list[dict[str, object]]:
        """SQL statements aggregated across stored traces, heaviest first."""
        schema = search.schema_for(
            "queries",
            {"query": "fp", "db": "alias", "since": "start", "until": "start"},
            free_text=lambda words: ("statement LIKE ? ESCAPE '\\'", [f"%{search._like(words)}%"]),  # noqa: SLF001
            custom={"table": lambda term: ("name LIKE ?", [f"% {term.value}"])},
        )
        where, params = search.compile_where(query, schema)
        with closing(self._connect()) as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT fp, alias, MAX(vendor) AS vendor, statement, name,"  # noqa: S608 (constant SQL; values are bound)
                    " COUNT(*) AS calls, COUNT(DISTINCT trace_id) AS traces,"
                    " SUM(duration) AS total, MAX(duration) AS slowest, MAX(start) AS last_seen"
                    f" FROM sql WHERE {where} GROUP BY fp ORDER BY total DESC LIMIT ?",
                    [*params, limit],
                )
            ]
            fps = [str(row["fp"]) for row in rows]
            if not fps:
                return []
            marks = ", ".join("?" * len(fps))
            per_trace = {
                str(row["fp"]): int(row["most"])
                for row in db.execute(
                    "SELECT fp, MAX(n) AS most FROM (SELECT fp, trace_id, COUNT(*) AS n FROM sql"  # noqa: S608 (constant SQL; values are bound)
                    f" WHERE fp IN ({marks}) GROUP BY fp, trace_id) GROUP BY fp",
                    fps,
                )
            }
            origins: dict[str, Counter[str]] = {}
            for row in db.execute(
                f"SELECT fp, origin FROM sql WHERE fp IN ({marks}) AND origin IS NOT NULL",  # noqa: S608
                fps,
            ):
                origins.setdefault(str(row["fp"]), Counter())[str(row["origin"])] += 1
            # The latest call of each statement: the console explains and opens it.
            samples = {
                str(row["fp"]): (str(row["trace_id"]), str(row["span_id"]))
                for row in db.execute(
                    "SELECT fp, trace_id, span_id, MAX(start) FROM sql"  # noqa: S608 (constant SQL; values are bound)
                    f" WHERE fp IN ({marks}) GROUP BY fp",
                    fps,
                )
            }
        for row in rows:
            fp = str(row["fp"])
            row["sample_trace"], row["sample_span"] = samples[fp]
            row["vendor"] = row["vendor"] or ""
            row["most_per_trace"] = per_trace.get(fp, 1)
            common = origins.get(fp, Counter()).most_common(1)
            row["origin"] = common[0][0] if common else None
        return rows

    def list_endpoints(self, query: str = "", *, limit: int = 100) -> list[dict[str, object]]:
        """HTTP traffic per route: calls, latency percentiles, queries per call, issues, errors."""
        schema = search.schema_for(
            "endpoints",
            {"route": "route", "since": "start", "until": "start"},
            free_text=lambda words: ("route LIKE ? ESCAPE '\\'", [f"%{search._like(words)}%"]),  # noqa: SLF001
            custom={"method": search.upper_text("method")},
        )
        where, params = search.compile_where(query, schema)
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        with closing(self._connect()) as db:
            for row in db.execute(
                "SELECT method, route, duration, queries, issues, errors, status, http_status, start,"  # noqa: S608 (constant SQL; values are bound)
                " CASE WHEN name LIKE 'GraphQL %' THEN name END AS operation FROM traces"
                f" WHERE kind = 'http' AND route IS NOT NULL AND {where}",
                params,
            ):
                groups.setdefault((str(row["method"]), str(row["route"])), []).append(row)
        endpoints: list[dict[str, object]] = []
        for (method, route), rows in groups.items():
            durations = sorted(int(row["duration"]) for row in rows)
            endpoints.append(
                {
                    "method": method,
                    "route": route,
                    "calls": len(rows),
                    "p50": durations[len(durations) // 2],
                    "p95": durations[min(len(durations) - 1, int(len(durations) * 0.95))],
                    "total": sum(durations),
                    "avg_queries": round(statistics.fmean(int(row["queries"]) for row in rows), 1),
                    "with_issues": sum(1 for row in rows if row["issues"]),
                    "errors": sum(1 for row in rows if _is_error(row)),
                    "last_seen": max(int(row["start"]) for row in rows),
                    # A GraphQL endpoint serves many operations: the GraphQL page breaks them down.
                    "graphql_operations": len({row["operation"] for row in rows if row["operation"]}),
                }
            )
        endpoints.sort(key=lambda item: int(str(item["total"])), reverse=True)
        return endpoints[:limit]

    def list_tasks(self, query: str = "", *, limit: int = 100) -> list[dict[str, object]]:
        """Celery tasks by name: runs, failures, retries, latency, time waiting in the queue; busiest first."""
        schema = search.schema_for(
            "tasks",
            {"task": "name", "since": "start", "until": "start"},
            free_text=lambda words: ("name LIKE ? ESCAPE '\\'", [f"%{search._like(words)}%"]),  # noqa: SLF001
            custom={
                "queue": search.text(f"json_extract(doc, '$.attrs.{QUEUE}')"),
                "state": search.upper_text(f"json_extract(doc, '$.attrs.{STATE}')"),
                "system": search.text(f"json_extract(doc, '$.attrs.{SYSTEM}')"),
            },
        )
        where, params = search.compile_where(query, schema)
        groups: dict[str, list[sqlite3.Row]] = {}
        with closing(self._connect()) as db:
            for row in db.execute(
                "SELECT name, duration, status, start, issues, queries,"  # noqa: S608 (constant SQL; values are bound)
                f" json_extract(doc, '$.attrs.{SYSTEM}') AS system,"
                f" json_extract(doc, '$.attrs.{QUEUE}') AS queue,"
                f" json_extract(doc, '$.attrs.{STATE}') AS state,"
                f" json_extract(doc, '$.attrs.{WAIT}') AS wait"
                f" FROM traces WHERE kind = 'task' AND {where}",
                params,
            ):
                groups.setdefault(str(row["name"]), []).append(row)
        tasks: list[dict[str, object]] = []
        for name, rows in groups.items():
            durations = sorted(int(row["duration"]) for row in rows)
            waits = sorted(int(row["wait"]) for row in rows if row["wait"] is not None)
            tasks.append(
                {
                    "task": name,
                    "system": str(rows[0]["system"] or "celery"),
                    "runs": len(rows),
                    "failures": sum(1 for row in rows if row["state"] == "FAILURE"),
                    "retries": sum(1 for row in rows if row["state"] == "RETRY"),
                    "p50": _pct(durations, 0.5),
                    "p95": _pct(durations, 0.95),
                    "total": sum(durations),
                    "wait_p50": _pct(waits, 0.5) if waits else None,
                    "wait_p95": _pct(waits, 0.95) if waits else None,
                    "avg_queries": round(statistics.fmean(int(row["queries"]) for row in rows), 1),
                    "with_issues": sum(1 for row in rows if row["issues"]),
                    "queues": sorted({str(row["queue"]) for row in rows if row["queue"]}),
                    "last_seen": max(int(row["start"]) for row in rows),
                }
            )
        tasks.sort(key=lambda item: int(str(item["total"])), reverse=True)
        return tasks[:limit]

    def list_websockets(self, query: str = "", *, limit: int = 100) -> list[dict[str, object]]:
        """WebSocket consumers: connections, messages, errors and handler time per consumer; busiest first."""
        event = "json_extract(doc, '$.attrs.\"ws.event\"')"
        schema = search.schema_for(
            "websockets",
            {"consumer": "view", "since": "start", "until": "start"},
            free_text=lambda words: (
                "(view LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')",
                [f"%{search._like(words)}%"] * 2,  # noqa: SLF001
            ),
            custom={"event": search.text(event)},
        )
        where, params = search.compile_where(query, schema)
        groups: dict[str, list[sqlite3.Row]] = {}
        with closing(self._connect()) as db:
            for row in db.execute(
                f"SELECT view, duration, status, start, issues, queries, {event} AS event,"  # noqa: S608 (constant SQL; values are bound)
                " json_extract(doc, '$.attrs.\"ws.path\"') AS ws_path"
                f" FROM traces WHERE kind = 'websocket' AND view IS NOT NULL AND {where}",
                params,
            ):
                groups.setdefault(str(row["view"]), []).append(row)
        consumers: list[dict[str, object]] = []
        for consumer, rows in groups.items():
            durations = sorted(int(row["duration"]) for row in rows)
            events = Counter(str(row["event"]) for row in rows)
            consumers.append(
                {
                    "consumer": consumer,
                    "connections": events["connect"],
                    "received": events["receive"],
                    "delivered": sum(count for name, count in events.items() if name not in WS_LIFECYCLE),
                    "disconnects": events["disconnect"],
                    "errors": sum(1 for row in rows if row["status"] == "error"),
                    "p50": _pct(durations, 0.5),
                    "p95": _pct(durations, 0.95),
                    "total": sum(durations),
                    "avg_queries": round(statistics.fmean(int(row["queries"]) for row in rows), 1),
                    "with_issues": sum(1 for row in rows if row["issues"]),
                    "paths": sorted({str(row["ws_path"]) for row in rows if row["ws_path"]})[:5],
                    "last_seen": max(int(row["start"]) for row in rows),
                }
            )
        consumers.sort(key=lambda item: int(str(item["total"])), reverse=True)
        return consumers[:limit]

    def list_graphql(self, query: str = "", *, limit: int = 100) -> list[dict[str, object]]:
        """GraphQL operations by name: calls, errors, latency and queries per call; busiest first."""
        operation = f"json_extract(doc, '$.attrs.{OPERATION}')"
        schema = search.schema_for(
            "graphql",
            {"since": "start", "until": "start"},
            free_text=lambda words: (f"{operation} LIKE ? ESCAPE '\\'", [f"%{search._like(words)}%"]),  # noqa: SLF001
            custom={
                "operation": search.text(operation),
                "type": search.text(f"json_extract(doc, '$.attrs.{OPERATION_TYPE}')"),
            },
        )
        where, params = search.compile_where(query, schema)
        groups: dict[str, list[sqlite3.Row]] = {}
        with closing(self._connect()) as db:
            for row in db.execute(
                f"SELECT {operation} AS operation, duration, status, start, issues, queries,"  # noqa: S608 (constant SQL; values are bound)
                f" json_extract(doc, '$.attrs.{OPERATION_TYPE}') AS type,"
                f" json_extract(doc, '$.attrs.{OPERATION_NAME}') AS name"
                f" FROM traces WHERE {operation} IS NOT NULL AND {where}",
                params,
            ):
                groups.setdefault(str(row["operation"]), []).append(row)
        operations: list[dict[str, object]] = []
        for name, rows in groups.items():
            durations = sorted(int(row["duration"]) for row in rows)
            operations.append(
                {
                    "operation": name,
                    "type": rows[0]["type"],
                    "name": rows[0]["name"],
                    "calls": len(rows),
                    "errors": sum(1 for row in rows if row["status"] == "error"),
                    "p50": _pct(durations, 0.5),
                    "p95": _pct(durations, 0.95),
                    "total": sum(durations),
                    "avg_queries": round(statistics.fmean(int(row["queries"]) for row in rows), 1),
                    "with_issues": sum(1 for row in rows if row["issues"]),
                    "last_seen": max(int(row["start"]) for row in rows),
                }
            )
        operations.sort(key=lambda item: int(str(item["total"])), reverse=True)
        return operations[:limit]

    def graphql_detail(self, operation: str, *, buckets: int = 40) -> dict[str, object] | None:
        """One GraphQL operation: like an endpoint (percentiles, series, issues, calls) plus its resolvers."""
        match = f"json_extract(doc, '$.attrs.{OPERATION}') = ?"
        detail = self._detail(match, [operation], buckets=buckets)
        if detail is None:
            return None
        with closing(self._connect()) as db:
            docs = [
                json.loads(row[0])
                for row in db.execute(f"SELECT doc FROM traces WHERE {match} ORDER BY start", (operation,))  # noqa: S608
            ]
        attrs = docs[-1]["attrs"]
        return {
            "operation": operation,
            "type": attrs.get("graphql.operation.type"),
            "name": attrs.get("graphql.operation.name"),
            "document": attrs.get("graphql.document"),
            **detail,
            "resolvers": _resolver_stats(docs),
        }

    def related(self, trace_id: str) -> "Related":
        """Traces linked to this one, by link kind: tasks (enqueued_by), WebSocket connections and layer messages."""
        with closing(self._connect()) as db:
            links = db.execute(
                "SELECT kind, linked_trace, linked_span, trace_id FROM links WHERE trace_id = ? OR linked_trace = ?",
                (trace_id, trace_id),
            ).fetchall()
        parents: dict[str, list[str]] = {}
        children: dict[str, list[str]] = {}
        # Which span of this trace each linked trace came from: the call tree shows a task under its enqueue.
        via: dict[str, str] = {}
        for kind, linked, span, owner in links:
            if owner == trace_id:
                parents.setdefault(kind, []).append(linked)
            else:
                children.setdefault(kind, []).append(owner)
                via[owner] = span

        def one(kind: str) -> dict[str, object] | None:
            found: list[dict[str, object]] = self.list_traces(ids=parents[kind]) if parents.get(kind) else []
            return found[0] if found else None

        def many(kind: str) -> list[dict[str, object]]:
            found: list[dict[str, object]] = (
                self.list_traces(ids=children[kind], limit=RELATED_LIMIT) if children.get(kind) else []
            )
            return sorted(found, key=lambda item: int(str(item["start"])))

        return {
            "enqueued_by": one("enqueued_by"),
            "enqueued": many("enqueued_by"),
            "connection": one("connection"),
            "connection_events": many("connection"),
            "sent_by": one("sent_by"),
            "delivered": many("sent_by"),
            "via": via,
        }

    def _detail(self, where: str, params: list[Param], *, buckets: int = 40) -> dict[str, object] | None:
        """A group of traces (an endpoint, an operation, a task…): summary, series, histogram, issues, calls."""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {LISTING} FROM traces WHERE {where} ORDER BY start",  # noqa: S608 (constant SQL; values are bound)
                params,
            ).fetchall()
            issues = [
                dict(row)
                for row in db.execute(
                    "SELECT issues.*, COUNT(issue_traces.trace_id) AS calls FROM issues"  # noqa: S608
                    " JOIN issue_traces ON issue_traces.fingerprint = issues.fingerprint"
                    f" WHERE issue_traces.trace_id IN (SELECT id FROM traces WHERE {where})"
                    " GROUP BY issues.fingerprint ORDER BY calls DESC",
                    params,
                )
            ]
        if not rows:
            return None
        traces = [self._trace_row(row) for row in rows]
        return {
            **_stats(rows, buckets),
            "issues": issues,
            "slowest": sorted(traces, key=lambda item: -int(str(item["duration"])))[:10],
            "recent": traces[::-1][:10],
        }

    def summary(self, query: str, *, buckets: int = 40) -> dict[str, object] | None:
        """Any group of traces, named by a search: `task:x`, `kind:command name:sync-prices`, `operation:"query X"`."""
        where, params = search.compile_where(query, TRACE_SEARCH)
        detail = self._detail(where, params, buckets=buckets)
        return {"query": query, **detail} if detail else None

    def endpoint_detail(self, method: str, route: str, *, buckets: int = 40) -> dict[str, object] | None:
        """One endpoint: percentile summary, time series, latency histogram, issues, slowest and recent calls."""
        detail = self._detail("kind = 'http' AND method = ? AND route = ?", [method.upper(), route], buckets=buckets)
        return {"method": method.upper(), "route": route, **detail} if detail else None

    def list_jobs(self, query: str = "", *, limit: int = 100) -> list[dict[str, object]]:
        """Jobs: management commands and `tracepanel.trace()` blocks (kind command) by name; busiest first."""
        source = f"CASE WHEN json_extract(doc, '$.attrs.{COMMAND}') IS NULL THEN 'custom' ELSE 'manage.py' END"
        schema = search.schema_for(
            "jobs",
            {"job": "name", "since": "start", "until": "start"},
            free_text=lambda words: ("name LIKE ? ESCAPE '\\'", [f"%{search._like(words)}%"]),  # noqa: SLF001
            custom={"source": search.text(source)},
        )
        where, params = search.compile_where(query, schema)
        groups: dict[str, list[sqlite3.Row]] = {}
        with closing(self._connect()) as db:
            for row in db.execute(
                f"SELECT name, duration, status, start, issues, queries, {source} AS source"  # noqa: S608
                f" FROM traces WHERE kind = 'command' AND {where}",
                params,
            ):
                groups.setdefault(str(row["name"]), []).append(row)
        jobs: list[dict[str, object]] = []
        for name, rows in groups.items():
            durations = sorted(int(row["duration"]) for row in rows)
            jobs.append(
                {
                    "job": name,
                    "source": rows[0]["source"],
                    "runs": len(rows),
                    "failures": sum(1 for row in rows if row["status"] == "error"),
                    "p50": _pct(durations, 0.5),
                    "p95": _pct(durations, 0.95),
                    "total": sum(durations),
                    "avg_queries": round(statistics.fmean(int(row["queries"]) for row in rows), 1),
                    "with_issues": sum(1 for row in rows if row["issues"]),
                    "last_seen": max(int(row["start"]) for row in rows),
                }
            )
        jobs.sort(key=lambda item: int(str(item["total"])), reverse=True)
        return jobs[:limit]

    def traces_overview(self, query: str = "", *, buckets: int = 60, group: str = "operation") -> dict[str, object]:
        """Monitoring over every trace matching a search, whatever its kind (requests, tasks, commands).

        Work is grouped into operations, a trace's kind plus name: `GET /posts/<int:pk>/`, a task name, a
        command; with `group="endpoint"` requests group by method + route instead (the HTTP page: all GraphQL
        operations are one endpoint there). The time series spans the searched window: `since:` (or the first
        match) up to `until:` (or now), so an empty stretch shows as such instead of vanishing.
        """
        where, params = search.compile_where(query, TRACE_SEARCH)
        bounds = {
            term.key: search.parse_time_us(term.value)
            for term in search.parse(query)
            if term.key in {"since", "until"} and term.op == ":"
        }
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {LISTING} FROM traces WHERE {where} ORDER BY start",  # noqa: S608
                params,
            ).fetchall()
        operations: dict[tuple[str, str], list[sqlite3.Row]] = {}
        kinds: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            # An empty route (older FastAPI traces of a router's root) is no route.
            by_endpoint = group == "endpoint" and row["kind"] == "http" and bool(row["route"])
            name = f"{row['method']} {row['route']}" if by_endpoint else str(row["name"])
            operations.setdefault((str(row["kind"]), name), []).append(row)
            kinds.setdefault(str(row["kind"]), []).append(row)
        stats = [
            {
                "kind": kind,
                "name": name,
                # Requests link to their endpoint page.
                "method": group[0]["method"] if kind == "http" else None,
                "route": group[0]["route"] if kind == "http" else None,
                **_group_stats(group),
            }
            for (kind, name), group in operations.items()
        ]
        return {
            **_stats(rows, buckets, start=bounds.get("since"), end=bounds.get("until", time.time_ns() // 1000)),
            "slow_threshold": SLOW_REQUEST_US,
            "kinds": [{"kind": kind, **_group_stats(group)} for kind, group in sorted(kinds.items())],
            "slowest": sorted(stats, key=lambda item: -int(str(item["p95"])))[:6],
            "failing": sorted((item for item in stats if item["errors"]), key=lambda item: -int(str(item["errors"])))[
                :6
            ],
        }

    def get_span(self, trace_id: str, span_id: str) -> dict[str, object] | None:
        trace = self.get_trace(trace_id)
        spans = trace.get("spans") if trace else None
        if not isinstance(spans, list):
            return None
        return next((span for span in spans if isinstance(span, dict) and span.get("id") == span_id), None)

    def list_mails(
        self, query: str = "", *, limit: int = 100, before: int | None = None
    ) -> tuple[list[dict[str, object]], int | None]:
        """Sent emails, newest first, with the request that sent them."""
        schema = search.schema_for(
            "mails",
            {"from": "mails.sender", "since": "mails.time", "until": "mails.time"},
            free_text=lambda words: (
                "mails.subject LIKE ? ESCAPE '\\' OR mails.recipients LIKE ? ESCAPE '\\'",
                [f"%{search._like(words)}%", f"%{search._like(words)}%"],  # noqa: SLF001
            ),
            custom={"to": lambda term: ("mails.recipients LIKE ? ESCAPE '\\'", [f"%,{search._like(term.value)},%"])},  # noqa: SLF001
        )
        where, params = search.compile_where(query, schema)
        if before is not None:
            where += " AND mails.time < ?"
            params.append(before)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT mails.*, traces.kind AS trace_kind, traces.htmx AS trace_htmx, traces.method, traces.path,"  # noqa: S608
                " traces.name AS trace_name FROM mails"
                f" JOIN traces ON traces.id = mails.trace_id WHERE {where} ORDER BY mails.time DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        items = [
            dict(row)
            | {
                "recipients": [address for address in str(row["recipients"]).split(",") if address],
                "sent": None if row["sent"] is None else bool(row["sent"]),
                "html": bool(row["html"]),
            }
            for row in rows[:limit]
        ]
        cursor = items[-1]["time"] if len(rows) > limit else None
        return items, cursor if isinstance(cursor, int) else None

    def known_values(self) -> dict[str, list[str]]:
        """Distinct routes, loggers, database aliases and extra= keys: what the console offers in menus and hints."""
        with closing(self._connect()) as db:

            def distinct(sql: str) -> list[str]:
                return [str(row[0]) for row in db.execute(sql)]

            return {
                "route": distinct("SELECT DISTINCT route FROM traces WHERE route != '' ORDER BY 1"),
                "logger": distinct("SELECT DISTINCT logger FROM logs ORDER BY 1"),
                "db": distinct("SELECT DISTINCT alias FROM sql ORDER BY 1"),
                "task": distinct("SELECT DISTINCT name FROM traces WHERE kind = 'task' ORDER BY 1"),
                "queue": distinct(
                    f"SELECT DISTINCT json_extract(doc, '$.attrs.{QUEUE}') FROM traces"  # noqa: S608 (constant SQL)
                    f" WHERE kind = 'task' AND json_extract(doc, '$.attrs.{QUEUE}') IS NOT NULL ORDER BY 1"
                ),
                "extra": distinct(
                    "SELECT DISTINCT json_each.key FROM logs, json_each(logs.attrs)"
                    " WHERE json_each.key LIKE 'extra.%' ORDER BY 1"
                ),
            }

    def list_issues(self, query: str = "", *, limit: int = 100) -> list[dict[str, object]]:
        """Issues (errors, N+1, duplicate and slow SQL) by fingerprint, most recent first, with their endpoints."""

        def route(term: Term) -> tuple[str, list[Param]]:
            clause, params = search.text("traces.route")(term)
            sql = (
                "fingerprint IN (SELECT issue_traces.fingerprint FROM issue_traces"  # noqa: S608 (clause is bound)
                f" JOIN traces ON traces.id = issue_traces.trace_id WHERE {clause})"
            )
            return sql, params

        schema = search.schema_for(
            "issues",
            {"kind": "kind", "since": "last_seen", "until": "last_seen"},
            free_text=lambda words: (
                "title LIKE ? ESCAPE '\\' OR culprit LIKE ? ESCAPE '\\'",
                [f"%{search._like(words)}%", f"%{search._like(words)}%"],  # noqa: SLF001
            ),
            custom={"route": route},
        )
        where, params = search.compile_where(query, schema)
        with closing(self._connect()) as db:
            rows = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM issues WHERE {where} ORDER BY last_seen DESC LIMIT ?",  # noqa: S608
                    [*params, limit],
                )
            ]
            for row in rows:
                row["endpoints"] = self._issue_endpoints(db, str(row["fingerprint"]))
        return rows

    def issue_detail(self, fingerprint: str, *, buckets: int = 40, limit: int = 50) -> dict[str, object] | None:
        """One issue: its group row, endpoints, events over time and the requests it happened in (newest first)."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM issues WHERE fingerprint = ?", (fingerprint,)).fetchone()
            if row is None:
                return None
            rows = db.execute(
                f"SELECT {LISTING} FROM traces WHERE id IN"  # noqa: S608 (constant SQL; values are bound)
                " (SELECT trace_id FROM issue_traces WHERE fingerprint = ?) ORDER BY start",
                (fingerprint,),
            ).fetchall()
            endpoints = self._issue_endpoints(db, fingerprint)
        series: object = _stats(rows, buckets, end=time.time_ns() // 1000)["series"] if rows else []
        return {
            **dict(row),
            "endpoints": endpoints,
            "series": series,
            "traces": [self._trace_row(trace) for trace in rows[::-1][:limit]],
        }

    @staticmethod
    def _issue_endpoints(db: sqlite3.Connection, fingerprint: str) -> list[str]:
        return [
            f"{endpoint['method']} {endpoint['route']}"
            for endpoint in db.execute(
                "SELECT DISTINCT traces.method, traces.route FROM issue_traces"
                " JOIN traces ON traces.id = issue_traces.trace_id"
                " WHERE issue_traces.fingerprint = ? AND traces.route IS NOT NULL ORDER BY 1, 2",
                (fingerprint,),
            )
        ]

    def list_services(self, query: str = "") -> dict[str, list[dict[str, object]]]:
        """What the project's web, workers and commands call, and how much: the nodes and edges of a service map."""
        schema = search.schema_for(
            "services",
            {"kind": "kind", "system": "system", "role": "role", "since": "start", "until": "start"},
            free_text=lambda words: (
                "(peer LIKE ? ESCAPE '\\' OR alias LIKE ? ESCAPE '\\')",
                [f"%{search._like(words)}%"] * 2,  # noqa: SLF001
            ),
        )
        where, params = search.compile_where(query, schema)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT service, role, peer, kind, system, address, port, namespace, direction,"  # noqa: S608 (constant SQL; values are bound)
                " GROUP_CONCAT(DISTINCT alias) AS aliases, COUNT(DISTINCT trace_id) AS traces, SUM(calls) AS calls,"
                " SUM(duration) AS total, SUM(errors) AS errors, MAX(start) AS last_seen"
                f" FROM peers WHERE {where} GROUP BY service, role, peer, direction",
                params,
            ).fetchall()
        nodes: dict[str, sqlite3.Row] = {}
        aliases: dict[str, set[str]] = {}
        totals: dict[str, Counter[str]] = {}
        apps: set[tuple[str, str]] = set()
        edges: list[dict[str, object]] = []
        for row in rows:
            peer = str(row["peer"])
            nodes.setdefault(peer, row)
            aliases.setdefault(peer, set()).update(str(row["aliases"]).split(",") if row["aliases"] else ())
            total = totals.setdefault(peer, Counter())
            if row["direction"] == "out":
                # A worker taking tasks is not a call to the broker: its queue wait stays on the edge.
                total.update({key: int(row[key]) for key in ("calls", "total", "errors")})
            total["last_seen"] = max(total["last_seen"], int(row["last_seen"]))
            apps.add((str(row["service"]), str(row["role"])))
            edges.append(
                {
                    "service": row["service"],
                    "role": row["role"],
                    "peer": peer,
                    "direction": row["direction"],
                    **{key: int(row[key]) for key in ("traces", "calls", "total", "errors")},
                }
            )
        services_ = [
            {
                **{key: row[key] for key in ("kind", "system", "address", "port", "namespace")},
                "id": peer,
                "aliases": sorted(aliases[peer]),
                **{key: totals[peer][key] for key in ("calls", "total", "errors", "last_seen")},
            }
            for peer, row in nodes.items()
        ]
        return {
            "apps": [{"service": name, "role": role} for name, role in sorted(apps, key=lambda app: (app[1], app[0]))],
            "services": sorted(services_, key=lambda node: (str(node["kind"]), -totals[str(node["id"])]["total"])),
            "edges": sorted(edges, key=lambda edge: -int(str(edge["total"]))),
        }

    def counts(self) -> dict[str, object]:
        """Issue groups (and how many are errors) and traces per entry point: what the console's menu shows."""
        with closing(self._connect()) as db:
            kinds = {str(row[0]): int(row[1]) for row in db.execute("SELECT kind, COUNT(*) FROM issues GROUP BY kind")}
            traces = {str(row[0]): int(row[1]) for row in db.execute("SELECT kind, COUNT(*) FROM traces GROUP BY kind")}
            # A GraphQL request is named after its operation: the name says it, no JSON to parse.
            (graphql,) = db.execute(
                "SELECT COUNT(*) FROM traces WHERE kind = 'http' AND name LIKE 'GraphQL %'"
            ).fetchone()
        return {
            # Two lists in the console: what failed, and what could be better.
            "issues": sum(kinds.values()) - kinds.get("error", 0),
            "errors": kinds.get("error", 0),
            # Stored traces per entry point: the console hides the ones a project doesn't use.
            "sections": {
                "http": traces.get("http", 0) - int(graphql),
                "graphql": int(graphql),
                "tasks": traces.get("task", 0),
                "websockets": traces.get("websocket", 0),
                "jobs": traces.get("command", 0),
            },
        }


_store: Store | None = None


def get_store() -> Store:
    """The project's store, created on first use so management commands don't touch .tracepanel/."""
    global _store  # noqa: PLW0603 (lazy process-wide singleton)
    if _store is None:
        config = conf.get_config()
        # One file per store format: processes running different tracepanel versions (a web server and a worker
        # mid-upgrade) never recreate each other's tables.
        _store = Store(conf.project_root() / config.dir / f"traces.v{STORE_VERSION}.sqlite3", config.max_traces)
    return _store
