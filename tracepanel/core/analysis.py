"""In-process analysis for standalone mode: SQL fingerprints, N+1 and duplicate detection, error grouping.

The Rust agent implements the same rules; both are checked against the same fixtures.
"""

import hashlib
import re
from collections import Counter, defaultdict

from tracepanel.core.model import Issue, Span, Trace

FINGERPRINT_VERSION = 1
# Identical statement and parameters this many times in one trace.
DUPLICATES_MIN = 2

_WHITESPACE = re.compile(r"\s+")
_IN_LIST = re.compile(r"\bIN \((?:%s|\?)(?:, ?(?:%s|\?))*\)", re.IGNORECASE)
_BY_PK = re.compile(r'WHERE [`"]?\w+[`"]?\.[`"]?id[`"]? = (?:%s|\?)', re.IGNORECASE)

SELECT_RELATED = (
    "Load the related object in the same query: add `select_related()` for this ForeignKey "
    "to the queryset that produced these rows."
)
PREFETCH_RELATED = (
    "Load the related rows in one extra query: add `prefetch_related()` for this reverse "
    "or many-to-many relation to the queryset that produced these rows."
)
# The N+1 fix in the words of the database layer that ran the queries (`db.client` on the spans).
N_PLUS_ONE_HINTS = {
    ("django", True): SELECT_RELATED,
    ("django", False): PREFETCH_RELATED,
    ("sqlalchemy", True): (
        "Load the related object in the same query: add `joinedload()` for this relationship "
        "to the select() that produced these rows (or `selectinload()` for one extra query)."
    ),
    ("sqlalchemy", False): (
        "Load the related rows in one extra query: add `selectinload()` for this relationship "
        "to the select() that produced these rows."
    ),
}
N_PLUS_ONE = "Load the related rows with the parent rows (a join, or one batched query) instead of one query per row."
DUPLICATES = "Run the query once and reuse the result, or cache it for the request."
DATALOADER = (
    " In GraphQL, a resolver runs once per object: batch these lookups with a DataLoader "
    "(strawberry.dataloader), or prefetch in the parent resolver."
)
SLOW_SQL = "Check the query plan (`EXPLAIN`) and add an index or narrow the query."
LONG_TRANSACTION = (
    "Rows locked inside stay locked until COMMIT: keep slow work (HTTP calls, emails, heavy Python) "
    "out of the `atomic()` block."
)
LONG_TRANSACTION_SQLALCHEMY = (
    "Rows locked inside stay locked until COMMIT: keep slow work (HTTP calls, emails, heavy Python) out of the "
    "transaction: commit first, or do it before the `with session.begin()` / transaction block."
)
# For transactions SQLAlchemy opened (and what runs on it, such as sqlakit): there is no on_commit().
IN_TRANSACTION_SQLALCHEMY = {
    "task.enqueue": (
        "A worker can pick the task up before COMMIT and not see the data, or run it after a rollback: "
        "enqueue it after the commit (after the transaction block, or in an `after_commit` session event)."
    ),
    "mail": "The email goes out even if the transaction rolls back: send it after the commit.",
    "http.client": (
        "The transaction stays open, holding its locks, while the other service answers: make the call before "
        "the transaction, or after the commit."
    ),
}
# What happens too early when done before COMMIT, and the fix.
IN_TRANSACTION = {
    "task.enqueue": (
        "Task enqueued inside a transaction",
        (
            "A worker can pick the task up before COMMIT and not see the data, or run it after a rollback: "
            "enqueue it with `transaction.on_commit(lambda: task.delay(...))`."
        ),
    ),
    "mail": (
        "Email sent inside a transaction",
        "The email goes out even if the transaction rolls back: send it with `transaction.on_commit(...)`.",
    ),
    "http.client": (
        "HTTP call inside a transaction",
        (
            "The transaction stays open, holding its locks, while the other service answers: make the call "
            "before `atomic()` or in `transaction.on_commit(...)`."
        ),
    ),
}


def fingerprint(sql: str) -> str:
    """Normalize a parametrized query so the same statement maps to one fingerprint."""
    sql = _WHITESPACE.sub(" ", sql).strip()
    return _IN_LIST.sub("IN (...)", sql)


def issue_key(kind: str, *parts: str | None) -> str:
    raw = "|".join([str(FINGERPRINT_VERSION), kind, *(p or "" for p in parts)])
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


def _culprit(spans: list[Span]) -> str | None:
    """Where in project code: the template that issued the queries, if any, rendered from the view's line."""
    origins = Counter(o for s in spans if isinstance(o := s.attrs.get("code.origin"), str))
    origin = origins.most_common(1)[0][0] if origins else None
    templates = Counter(t for s in spans if isinstance(t := s.attrs.get("code.template"), str))
    if templates:
        template = templates.most_common(1)[0][0]
        return f"{template} (rendered from {origin})" if origin else template
    return origin


def _culprit_key(culprit: str | None) -> str | None:
    # "blog/views.py:21 in post_list" -> "blog/views.py in post_list": line numbers shift with edits.
    return re.sub(r":\d+ in ", " in ", culprit) if culprit else None


def _sql_issues(trace: Trace, n_plus_one_threshold: int, slow_sql_ms: int) -> list[Issue]:
    issues: list[Issue] = []
    # The same statement on two databases is two different queries.
    groups: dict[tuple[str, str], list[Span]] = defaultdict(list)
    for span in trace.spans:
        if span.kind == "sql":
            groups[(str(span.attrs.get("db.alias") or "default"), str(span.attrs.get("db.fingerprint")))].append(span)

    graphql = "graphql.operation" in trace.attrs
    for (alias, statement), spans in groups.items():
        key = statement if alias == "default" else f"{alias}|{statement}"
        culprit = _culprit(spans)
        by_params = Counter(s.attrs.get("db.params_hash") for s in spans)
        by_pk = bool(_BY_PK.search(statement))
        select = statement.upper().startswith("SELECT")
        # Only actionable patterns: reads that project code triggers. Django internals
        # (the admin's repeated COUNT, transaction statements) are not the user's to fix.
        actionable = culprit is not None and select
        # One exception: a loop of by-key lookups run entirely by a library (a nested DRF serializer with
        # source="author.username") is still the view's N+1, fixed by select_related / prefetch_related.
        if culprit is None and select and by_pk and len(by_params) >= n_plus_one_threshold:
            view = trace.attrs.get("code.view")
            culprit = f"{view} (in library code)" if isinstance(view, str) else None
            actionable = culprit is not None
        # A loop over rows that follows a relation is an N+1 even when every row points at the same object
        # (identical params): the same by-key lookup from one line of code, many times. Other identical repeats
        # (a COUNT in a loop, the same query from different places) are duplicates.
        origins = {s.attrs.get("code.origin") for s in spans}
        looped = len(spans) >= n_plus_one_threshold and len(origins) == 1 and by_pk
        if actionable and (len(by_params) >= n_plus_one_threshold or looped):
            issues.append(
                Issue(
                    fingerprint=issue_key("n+1", key, _culprit_key(culprit)),
                    fingerprint_version=FINGERPRINT_VERSION,
                    kind="n+1",
                    title=f"N+1: {len(spans)} similar queries · {spans[0].name}",
                    culprit=culprit,
                    hint=N_PLUS_ONE_HINTS.get((str(spans[0].attrs.get("db.client")), by_pk), N_PLUS_ONE)
                    + (DATALOADER if graphql else ""),
                    count=len(spans),
                    span_ids=[s.id for s in spans][:50],
                )
            )
        elif actionable and by_params.most_common(1)[0][1] >= DUPLICATES_MIN:
            repeated = [s for s in spans if by_params[s.attrs.get("db.params_hash")] >= DUPLICATES_MIN]
            issues.append(
                Issue(
                    fingerprint=issue_key("duplicates", key, _culprit_key(culprit)),
                    fingerprint_version=FINGERPRINT_VERSION,
                    kind="duplicates",
                    title=f"Duplicate queries: {len(repeated)} identical · {spans[0].name}",
                    culprit=culprit,
                    hint=DUPLICATES,
                    count=len(repeated),
                    span_ids=[s.id for s in repeated][:50],
                )
            )

        slow = [s for s in spans if s.duration >= slow_sql_ms * 1000]
        if slow:
            issues.append(
                Issue(
                    fingerprint=issue_key("slow-sql", key, _culprit_key(culprit)),
                    fingerprint_version=FINGERPRINT_VERSION,
                    kind="slow-sql",
                    title=f"Slow query: {max(s.duration for s in slow) / 1000:.0f} ms · {spans[0].name}",
                    culprit=_culprit(slow),
                    hint=SLOW_SQL,
                    count=len(slow),
                    span_ids=[s.id for s in slow][:50],
                )
            )
    return issues


TITLE_LIMIT = 200


def headline(message: str, limit: int = TITLE_LIMIT) -> str:
    """An error's message as an issue title: its first line (drivers add details below it), on one line, cut at a word.

    The full message stays on the error itself.
    """
    first = next((line for line in message.splitlines() if line.strip()), "")
    text = _WHITESPACE.sub(" ", first).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    return f"{(cut[:space] if space > limit // 2 else cut).rstrip(' ,.:;')}…"


def _error_issues(trace: Trace) -> list[Issue]:
    issues: list[Issue] = []
    for error in trace.errors:
        frame = next((f for f in reversed(error.frames) if f.in_app), error.frames[-1] if error.frames else None)
        culprit = f"{frame.filename}:{frame.lineno} in {frame.function}" if frame else None
        location = f"{frame.module}.{frame.function}" if frame else None
        issues.append(
            Issue(
                fingerprint=issue_key("error", error.type, location),
                fingerprint_version=FINGERPRINT_VERSION,
                kind="error",
                title=headline(f"{error.type.removeprefix('builtins.')}: {error.message}"),
                culprit=culprit,
                hint=None,
                count=1,
                span_ids=[error.span_id] if error.span_id else [],
            )
        )
    return issues


def _in_transaction(trace: Trace) -> dict[str, Span]:
    """Span id -> the outer transaction it ran in; on_commit callbacks (run after COMMIT) don't count."""
    by_id = {span.id: span for span in trace.spans}
    found: dict[str, Span] = {}
    for span in trace.spans:
        outer = None
        parent = by_id.get(span.parent_id or "")
        while parent is not None:
            if parent.attrs.get("db.transaction.on_commit"):
                outer = None
                break
            if parent.kind == "transaction" and not parent.attrs.get("db.transaction.savepoint"):
                outer = parent
            parent = by_id.get(parent.parent_id or "")
        if outer is not None:
            found[span.id] = outer
    return found


def _transaction_issues(trace: Trace, long_transaction_ms: int) -> list[Issue]:
    issues: list[Issue] = []
    for span in trace.spans:
        outer = span.kind == "transaction" and not span.attrs.get("db.transaction.savepoint")
        if outer and span.duration >= long_transaction_ms * 1000:
            culprit = span.attrs.get("code.origin")
            culprit = culprit if isinstance(culprit, str) else None
            issues.append(
                Issue(
                    fingerprint=issue_key("long-transaction", str(span.attrs.get("db.alias")), _culprit_key(culprit)),
                    fingerprint_version=FINGERPRINT_VERSION,
                    kind="long-transaction",
                    title=f"Long transaction: {span.duration / 1000:.0f} ms on {span.attrs.get('db.alias')}",
                    culprit=culprit,
                    hint=LONG_TRANSACTION_SQLALCHEMY
                    if span.attrs.get("db.client") == "sqlalchemy"
                    else LONG_TRANSACTION,
                    count=1,
                    span_ids=[span.id],
                )
            )
    groups: dict[tuple[str, str | None, str], list[Span]] = defaultdict(list)
    for span_id, outer in _in_transaction(trace).items():
        span = next(s for s in trace.spans if s.id == span_id)
        if span.kind in IN_TRANSACTION and not span.attrs.get("task.eager"):
            groups[(span.kind, _culprit([span]), str(outer.attrs.get("db.client")))].append(span)
    for (kind, culprit, client), spans in groups.items():
        title, hint = IN_TRANSACTION[kind]
        if client == "sqlalchemy":
            hint = IN_TRANSACTION_SQLALCHEMY[kind]
        issues.append(
            Issue(
                fingerprint=issue_key("in-transaction", kind, _culprit_key(culprit)),
                fingerprint_version=FINGERPRINT_VERSION,
                kind="in-transaction",
                title=f"{title} · {spans[0].name}",
                culprit=culprit,
                hint=hint,
                count=len(spans),
                span_ids=[s.id for s in spans][:50],
            )
        )
    return issues


def analyze(
    trace: Trace, *, n_plus_one_threshold: int = 3, slow_sql_ms: int = 100, long_transaction_ms: int = 500
) -> list[Issue]:
    """Detect issues in a finished trace; errors first, then by occurrence count."""
    found = _sql_issues(trace, n_plus_one_threshold, slow_sql_ms) + _transaction_issues(trace, long_transaction_ms)
    return _error_issues(trace) + sorted(found, key=lambda i: -i.count)
