"""The console under tracepanel's prefix, for any framework: the agent serves it, the app adds what only it can do.

A Request in, a Response out: the framework integration adapts its own request and response (Django:
tracepanel.django.console) and registers what needs the framework with register() (switching the user, EXPLAIN on
its databases). Everything else, the UI, widget.min.js and the API, comes from the project's agent (console.proxy).
"""

import html
import json
from collections.abc import Callable

from tracepanel.console import proxy
from tracepanel.console.messages import Request, Response
from tracepanel.core import conf, explain
from tracepanel.storage import agent

Handler = Callable[[Request], Response]


def json_response(data: object, status: int = 200) -> Response:
    return Response(status=status, body=json.dumps(data, separators=(",", ":")).encode())


NOT_FOUND = {"error": "not found"}
SERVICE_UNAVAILABLE = 503

# Registered by the framework integration: what needs the framework (sessions, its database connections).
EXTENSIONS: dict[str, Handler] = {}
# Its writes (POST): switching the user.
WRITES: dict[str, Handler] = {}


# Runs EXPLAIN for a captured statement on the database it came from: (statement, params, alias, analyze) -> plan.
ExplainRunner = Callable[..., explain.Plan]


def explain_handler(run: ExplainRunner) -> Handler:
    """`api/explain?trace=&span=&analyze=` for a framework's (or database layer's) way of running EXPLAIN."""

    def handle(request: Request) -> Response:
        span = _span(request.query.get("trace", ""), request.query.get("span", ""))
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


def _span(trace_id: str, span_id: str) -> dict[str, object] | None:
    document = proxy.trace(trace_id) if trace_id else None
    spans = document.get("spans") if document else None
    if not isinstance(spans, list):
        return None
    return next((span for span in spans if isinstance(span, dict) and span.get("id") == span_id), None)


# What the app adds to its agent's console beyond EXPLAIN (Django: switching users), each checked when asked.
CAPABILITIES: dict[str, Callable[[], bool]] = {}


def capability(name: str, check: Callable[[], bool]) -> None:
    """A feature only the framework offers, told to the console by `api/capabilities` so it shows it only then."""
    CAPABILITIES[name] = check


def capabilities() -> dict[str, bool]:
    """The agent answers none of these: the console shows EXPLAIN, or a user switcher, only where the app runs them."""
    added = {name: check() for name, check in CAPABILITIES.items()}
    return {"explain": "explain" in EXTENSIONS, "users": False} | added


def register(path: str, handler: Handler, *, write: bool = False) -> None:
    """Add an API resource (`api/<path>`) only the framework can serve; `write` ones accept POST."""
    (WRITES if write else EXTENSIONS)[path] = handler


register("capabilities", lambda _request: json_response(capabilities()))


def dispatch(request: Request) -> Response:
    if any(part in {".", ".."} for part in request.path.split("/")):
        return json_response(NOT_FOUND, status=404)
    path = request.path.removeprefix("api/") if request.path.startswith("api/") else None
    if path is not None and (handler := WRITES.get(path)):
        return handler(request)
    if path is not None and (handler := EXTENSIONS.get(path)):
        if request.method != "GET":
            return json_response({"error": "method not allowed"}, status=405)
        return handler(request)
    if request.method != "GET" and (refused := _refused_write(request)):
        return refused
    return proxy.forward(request, request.path) or _no_agent(api=path is not None)


def _refused_write(request: Request) -> Response | None:
    """A write to the agent (a page's traces, clearing the store) from elsewhere than this machine, or too big."""
    if not request.local:
        return json_response({"error": "the console takes writes from this machine only"}, status=403)
    if request.too_large:
        return json_response({"error": "too large"}, status=413)
    return None


def _no_agent(*, api: bool) -> Response:
    """The console without an agent: why, and what to do."""
    reason = agent.unavailable(conf.get_config())
    if api:
        return json_response({"error": f"No tracepanel agent: {reason}"}, status=SERVICE_UNAVAILABLE)
    page = (
        "<!doctype html><meta charset=utf-8><title>tracepanel</title>"
        "<body style='font-family: system-ui; max-width: 40rem; margin: 4rem auto; line-height: 1.5'>"
        f"<h1>tracepanel</h1><p>The console comes from the tracepanel agent, and {html.escape(reason)}</p>"
    )
    return Response(status=SERVICE_UNAVAILABLE, content_type="text/html; charset=utf-8", body=page.encode())
