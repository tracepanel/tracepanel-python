"""The console: the UI bundle, widget.js and the JSON API under tracepanel's prefix, for any framework.

A Request in, a Response out: the framework integration adapts its own request and response (Django:
tracepanel.django.console) and registers what only it can do with register() (switching the user,
EXPLAIN on its databases). The API is described in schema/openapi.yaml.
"""

import json
import mimetypes
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import IO

from tracepanel.core import explain
from tracepanel.storage import search, transport
from tracepanel.storage.search import SearchError
from tracepanel.storage.store import get_store

STATIC = files("tracepanel") / "static" / "tracepanel"
MAX_LIMIT = 500
WRITE_WAIT_S = 1.0

TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
}


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    # Below the prefix: "api/traces", "assets/index-abc.js", "" for the page.
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    # The framework's own request, for the handlers an integration registers.
    native: object = None


@dataclass(slots=True)
class Response:
    status: int = 200
    content_type: str = "application/json"
    body: bytes = b""
    # A static file, streamed.
    file: IO[bytes] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # A response the framework built itself (it may set cookies): returned as is.
    native: object = None


Handler = Callable[[Request], Response]


def json_response(data: object, status: int = 200) -> Response:
    return Response(status=status, body=json.dumps(data, separators=(",", ":")).encode())


NOT_FOUND = {"error": "not found"}


def ui(request: Request) -> Response:
    path = request.path
    if any(part in {"", ".", ".."} for part in path.split("/")[:-1]) or path.endswith(".."):
        return json_response(NOT_FOUND, status=404)
    if path == "widget.js":
        asset = STATIC / "widget.js"
    else:
        asset = STATIC / "ui" / path
        if not path or not asset.is_file():
            # Unknown paths fall back to index.html: the SPA handles routing.
            asset = STATIC / "ui" / "index.html"
    if not asset.is_file():
        return json_response({"error": "tracepanel UI is not installed: run `uv run poe ui` in tracepanel-python"}, 404)
    # Fixed types for what the bundle ships: mimetypes differs across Python versions and platforms.
    suffix = Path(asset.name).suffix
    content_type = TYPES.get(suffix) or mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
    # Built assets carry a content hash in their name; the page and the widget script do not, so a new
    # tracepanel version must never be hidden behind a cached copy of them.
    hashed = path.startswith("assets/") and asset.name != "index.html"
    cache = "public, max-age=31536000, immutable" if hashed else "no-cache"
    return Response(content_type=content_type, file=asset.open("rb"), headers={"Cache-Control": cache})


def _limit(request: Request) -> int:
    try:
        return max(1, min(int(request.query.get("limit", "100")), MAX_LIMIT))
    except ValueError:
        return 100


def _before(request: Request) -> int | None:
    value = request.query.get("before")
    return int(value) if value and value.isdigit() else None


def _traces(request: Request) -> Response:
    store = get_store()
    ids = [i for i in request.query.get("ids", "").split(",") if i]
    if ids:
        return json_response({"traces": store.list_traces(ids=ids, limit=_limit(request)), "next": None})
    traces, cursor = store.search_traces(request.query.get("q", ""), limit=_limit(request), before=_before(request))
    return json_response({"traces": traces, "next": cursor})


def _logs(request: Request) -> Response:
    logs, cursor = get_store().search_logs(request.query.get("q", ""), limit=_limit(request), before=_before(request))
    return json_response({"logs": logs, "next": cursor})


def _queries(request: Request) -> Response:
    return json_response({"queries": get_store().list_queries(request.query.get("q", ""), limit=_limit(request))})


def _mails(request: Request) -> Response:
    items, cursor = get_store().list_mails(request.query.get("q", ""), limit=_limit(request), before=_before(request))
    return json_response({"mails": items, "next": cursor})


def _tasks(request: Request) -> Response:
    return json_response({"tasks": get_store().list_tasks(request.query.get("q", ""), limit=_limit(request))})


def _services(request: Request) -> Response:
    return json_response(get_store().list_services(request.query.get("q", "")))


def _graphql(request: Request) -> Response:
    return json_response({"graphql": get_store().list_graphql(request.query.get("q", ""), limit=_limit(request))})


def _summary(request: Request) -> Response:
    detail = get_store().summary(request.query.get("q", ""))
    return json_response(detail) if detail else json_response(NOT_FOUND, status=404)


def _jobs(request: Request) -> Response:
    return json_response({"jobs": get_store().list_jobs(request.query.get("q", ""), limit=_limit(request))})


def _graphql_operation(request: Request) -> Response:
    detail = get_store().graphql_detail(request.query.get("operation", ""))
    return json_response(detail) if detail else json_response(NOT_FOUND, status=404)


def _websockets(request: Request) -> Response:
    return json_response({"websockets": get_store().list_websockets(request.query.get("q", ""), limit=_limit(request))})


def _endpoints(request: Request) -> Response:
    return json_response({"endpoints": get_store().list_endpoints(request.query.get("q", ""), limit=_limit(request))})


def _issues(request: Request) -> Response:
    return json_response({"issues": get_store().list_issues(request.query.get("q", ""), limit=_limit(request))})


def _issue(request: Request) -> Response:
    detail = get_store().issue_detail(request.query.get("fingerprint", ""))
    return json_response(detail) if detail else json_response({"error": "not found"}, status=404)


def _counts(request: Request) -> Response:  # noqa: ARG001
    return json_response(get_store().counts())


def _endpoint(request: Request) -> Response:
    detail = get_store().endpoint_detail(request.query.get("method", "GET"), request.query.get("route", ""))
    return json_response(detail) if detail else json_response({"error": "not found"}, status=404)


def _overview(request: Request) -> Response:
    group = request.query.get("group", "operation")
    if group not in {"operation", "endpoint"}:
        return json_response({"error": "group must be operation or endpoint"}, status=400)
    return json_response(get_store().traces_overview(request.query.get("q", ""), group=group))


def _values(request: Request) -> Response:  # noqa: ARG001
    return json_response(get_store().known_values())


def _search_spec(request: Request) -> Response:  # noqa: ARG001
    return json_response(search.spec())


RESOURCES = {
    "traces": _traces,
    "traces/overview": _overview,
    "logs": _logs,
    "queries": _queries,
    "endpoints": _endpoints,
    "tasks": _tasks,
    "services": _services,
    "websockets": _websockets,
    "graphql": _graphql,
    "jobs": _jobs,
    "traces/summary": _summary,
    "graphql/detail": _graphql_operation,
    "mails": _mails,
    "endpoints/detail": _endpoint,
    "issues": _issues,
    "issues/detail": _issue,
    "counts": _counts,
    "search": _search_spec,
    "values": _values,
}

# Registered by the framework integration: what needs the framework (sessions, its database connections).
EXTENSIONS: dict[str, Handler] = {}
# The only writes: everything else is read-only.
WRITES: dict[str, Handler] = {}


# Runs EXPLAIN for a captured statement on the database it came from: (statement, params, alias, analyze) -> plan.
ExplainRunner = Callable[..., explain.Plan]


def explain_handler(run: ExplainRunner) -> Handler:
    """`api/explain?trace=&span=&analyze=` for a framework's (or database layer's) way of running EXPLAIN."""

    def handle(request: Request) -> Response:
        span = get_store().get_span(request.query.get("trace", ""), request.query.get("span", ""))
        attrs = span.get("attrs") if span else None
        if span is None or span.get("kind") != "sql" or not isinstance(attrs, dict):
            return json_response(NOT_FOUND, status=404)
        try:
            plan = run(
                str(attrs.get("db.statement", "")),
                explain.params_from(attrs),
                str(attrs.get("db.alias") or "default"),
                analyze=request.query.get("analyze") == "1",
            )
        except explain.ExplainError as exc:
            return json_response({"error": str(exc)}, status=422)
        return json_response(plan.to_dict())

    return handle


def register(path: str, handler: Handler, *, write: bool = False) -> None:
    """Add an API resource (`api/<path>`) only the framework can serve; `write` ones accept POST."""
    (WRITES if write else EXTENSIONS)[path] = handler


def _trace(path: str) -> Response | None:
    """traces/<id> and traces/<id>/related."""
    match = re.fullmatch(r"traces/([0-9A-Z]{26})(/related)?", path)
    if match is None:
        return None
    if match.group(2):
        return json_response(get_store().related(match.group(1)))
    trace = get_store().get_trace(match.group(1))
    if trace is None:
        # The widget asks as soon as the response arrives, while the background writer may still hold the trace
        # (it writes in batches): wait for this process's queue before answering 404.
        transport.get_writer().flush(timeout=WRITE_WAIT_S)
        trace = get_store().get_trace(match.group(1))
    return json_response(trace) if trace else json_response(NOT_FOUND, status=404)


def api(request: Request, path: str) -> Response:
    if handler := WRITES.get(path):
        return handler(request)
    if request.method != "GET":
        return json_response({"error": "method not allowed"}, status=405)
    try:
        handler = RESOURCES.get(path) or EXTENSIONS.get(path)
        response = handler(request) if handler else _trace(path)
    except SearchError as exc:
        return json_response({"error": str(exc)}, status=400)
    return response or json_response(NOT_FOUND, status=404)


def dispatch(request: Request) -> Response:
    if request.path.startswith("api/"):
        return api(request, request.path.removeprefix("api/"))
    return ui(request)
