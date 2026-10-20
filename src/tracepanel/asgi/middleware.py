"""Requests as traces under ASGI: a plain ASGI middleware, for any ASGI app.

Starlette and FastAPI put the matched route and endpoint in the scope: the trace is named after the route's template
and the endpoint runs inside a `view` span. Other frameworks' requests are named after their path.

The response's start is held until its body is complete (a normal response): its headers then carry the trace's
timing for the widget, and an HTML page gets widget.min.js. A body in several messages is still a normal response when
its Content-Length is declared (Starlette's BaseHTTPMiddleware sends every response so); without one it is a stream,
and goes out at once, with Tracepanel-Id only.
"""

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qsl

from tracepanel.console import app as console
from tracepanel.console import terminal, widget
from tracepanel.console.messages import MAX_BODY, is_local
from tracepanel.core import conf, errors, profile, w3c
from tracepanel.core import request as details
from tracepanel.core.context import current_trace, start_span, start_trace
from tracepanel.core.model import Attrs, Trace
from tracepanel.integrations import htmx
from tracepanel.storage.transport import complete

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

CHUNK = 64 * 1024
# A body in several messages is held whole up to this size (a declared Content-Length); larger ones are streamed.
MAX_HELD = 8 << 20
MARKER = "_tracepanel"


def _headers(message: Message) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in message.get("headers", [])}


def _set_header(message: Message, name: str, value: str) -> None:
    lowered = name.lower().encode("latin-1")
    headers = [(key, val) for key, val in message.get("headers", []) if key.lower() != lowered]
    message["headers"] = [*headers, (name.encode("latin-1"), value.encode("latin-1"))]


def _api(scope: Scope, prefix: str) -> str:
    return str(scope.get("root_path", "")).rstrip("/") + prefix


def _origin(scope: Scope) -> str:
    host = _headers({"headers": scope.get("headers", [])}).get("host")
    server = scope.get("server") or ("localhost", None)
    return f"{scope.get('scheme', 'http')}://{host or f'{server[0]}:{server[1]}'}"


def _template(scope: Scope) -> str | None:
    """The matched route's full path template, prefixes of included routers and mounts included."""
    # FastAPI 0.14x includes routers lazily: scope["route"] is the router's own route, its path relative to the
    # router's prefix (Strawberry's GraphQLRouter: ""). The full path is on the route context it records.
    context = (scope.get("fastapi") or {}).get("effective_route_context")
    route = scope.get("route")
    for candidate in (getattr(context, "path_format", None), getattr(route, "path_format", None)):
        if isinstance(candidate, str) and candidate:
            return str(scope.get("root_path", "")).rstrip("/") + candidate
    relative = getattr(route, "path", None)
    if relative == "":
        # A route at its router's root: the request's path is the route (it has no parameters).
        return str(scope.get("path", "")) or None
    return relative if isinstance(relative, str) else None


def _name(trace: Trace, scope: Scope, method: str) -> None:
    """The route and endpoint FastAPI matched (it puts them in the scope), else the path; GraphQL by operation."""
    template = _template(scope)
    if isinstance(template, str):
        trace.attrs["http.route"] = template
        trace.name = f"{method} {template}"
    endpoint = scope.get("endpoint")
    if endpoint is not None and hasattr(endpoint, "__qualname__"):
        trace.attrs["code.view"] = _endpoint_name(endpoint)
    operation = trace.attrs.get("graphql.operation")
    if isinstance(operation, str):
        trace.name = f"GraphQL {operation}"


def _endpoint_name(endpoint: object) -> str:
    return f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__qualname__', repr(endpoint))}"


def _view(route_app: ASGIApp, name: str) -> ASGIApp:
    """A route's ASGI app inside a `view` span: dependencies, the endpoint and serialization, as Django's view."""

    async def view(scope: Scope, receive: Receive, send: Send) -> None:
        with start_span("view", name, {"code.function": name}):
            await route_app(scope, receive, send)

    setattr(view, MARKER, True)
    return view


def wrap_routes(routes: list[Any]) -> None:
    """Every route's endpoint as a `view` span, inside mounts too; routes added later are wrapped on the next call."""
    for route in routes:
        nested = getattr(route, "routes", None)
        if isinstance(nested, list) and nested is not routes:
            # Mount and Host: their own routes (a mounted Starlette app's included).
            wrap_routes(nested)
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            # A router FastAPI 0.14x includes lazily: each of its routes has a context of its own (the prefix and
            # dependencies of the include), whose app (or Starlette route) runs instead of the router's route.
            wrap_routes([context.starlette_route or context for context in contexts()])
            continue
        endpoint = getattr(route, "endpoint", None)
        route_app = getattr(route, "app", None)
        if endpoint is None or route_app is None or getattr(route_app, MARKER, False):
            continue
        if getattr(route, "methods", None) is None:
            # WebSocket routes and mounts: not requests.
            continue
        route.app = _view(route_app, _endpoint_name(endpoint))


def _session(scope: Scope) -> Attrs:
    """The session Starlette's SessionMiddleware put on the scope, as the app left it; none without that middleware."""
    session = scope.get("session")
    return details.session(session.items()) if isinstance(session, dict) else {}


def _user(scope: Scope) -> Attrs:
    """The user Starlette's AuthenticationMiddleware put on the scope (its BaseUser); none without that middleware."""
    found = scope.get("user")
    if found is None or not hasattr(found, "is_authenticated"):
        return {}
    if not found.is_authenticated:
        return details.user(None, authenticated=False)
    name = getattr(found, "display_name", None)
    try:
        identity = found.identity
    except NotImplementedError:
        # SimpleUser has no identity: its name is all it has.
        identity = name
    return details.user(identity, name)


class TracepanelMiddleware:
    """`routes` returns the app's routes (Starlette's router), so endpoints can be wrapped as view spans."""

    def __init__(self, app: ASGIApp, routes: Callable[[], list[Any]] | None = None) -> None:
        self.app = app
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        config = conf.get_config()
        path = str(scope.get("path", ""))
        if path.startswith(config.prefix):
            await self._console(scope, receive, send, path.removeprefix(config.prefix))
            return
        if not config.enabled or conf.matches(path, config.ignore_paths) or current_trace() is not None:
            # Already traced by an app this one is mounted in (a Starlette app inside FastAPI): one trace per request.
            await self.app(scope, receive, send)
            return
        if self.routes is not None:
            # Cheap (a marker per route), and catches routes added or mounted after startup.
            wrap_routes(self.routes())
        method = str(scope.get("method", "GET"))
        failure: Exception | None = None
        headers = _headers(scope)
        attrs = {
            "http.method": method,
            "http.path": path,
            **htmx.request_attrs(headers),
            # Called by another app: the W3C trace its request carried.
            **w3c.incoming(headers.get(w3c.HEADER)),
        }
        reader = _BodyReader(receive) if config.capture_request else None
        if reader is not None:
            attrs.update(_request_details(scope))
        asked = profile.requested(headers.get(profile.HEADER), headers.get("cookie"))
        with start_trace("http", f"{method} {path}", attrs, profile=asked) as trace:
            responder = _Responder(scope, send, trace, config)
            try:
                await self.app(scope, receive if reader is None else reader.receive, responder.send)
            except Exception as exc:  # noqa: BLE001 (recorded, then raised again for the server to log)
                errors.capture(exc, handled=False)
                trace.status = "error"
                trace.attrs["http.status_code"] = 500
                failure = exc
                # Starlette's own 500 page goes out past this middleware, without tracepanel's headers: send the same
                # plain response with them, so the widget (and a fetch() caller) can open the failed request.
                await responder.error()
            _name(trace, scope, method)
            trace.attrs.update(_user(scope))
            if reader is not None:
                trace.attrs.update(reader.attrs(headers.get("content-type", "")))
                trace.attrs.update(_session(scope))
        complete(trace)
        if failure is not None:
            raise failure

    async def _console(self, scope: Scope, receive: Receive, send: Send, path: str) -> None:
        method = str(scope.get("method", "GET"))
        body, too_large = await _read_body(receive) if method == "POST" else (b"", False)
        headers = dict(scope.get("headers", []))
        client = scope.get("client") or ("", 0)
        request = console.Request(
            method=method,
            path=path,
            query=dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"))),
            body=body,
            content_type=headers.get(b"content-type", b"").decode("latin-1"),
            local=is_local(str(client[0])),
            too_large=too_large,
        )
        # The console reads SQLite (and may wait for the writer): off the event loop.
        response = await asyncio.to_thread(console.dispatch, request)
        headers = [
            (b"content-type", response.content_type.encode()),
            *((name.lower().encode(), value.encode()) for name, value in response.headers.items()),
        ]
        await send({"type": "http.response.start", "status": response.status, "headers": headers})
        if response.file is None:
            await send({"type": "http.response.body", "body": response.body})
            return
        file = response.file
        try:
            while chunk := await asyncio.to_thread(file.read, CHUNK):
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        finally:
            file.close()
        await send({"type": "http.response.body", "body": b""})


def _request_details(scope: Scope) -> dict[str, Any]:
    pairs = [(key.decode("latin-1"), value.decode("latin-1")) for key, value in scope.get("headers", [])]
    return {
        **details.request_headers(pairs),
        **details.query(scope.get("query_string", b"").decode("latin-1")),
        **details.cookies(", ".join(value for name, value in pairs if name.lower() == "cookie")),
    }


class _BodyReader:
    """Passes the app the messages it asks for, keeping the start of the body they carry: only what the app read."""

    def __init__(self, receive: Receive) -> None:
        self.upstream = receive
        self.chunks: list[bytes] = []
        self.size = 0
        self.complete = False

    async def receive(self) -> Message:
        message = await self.upstream()
        if message.get("type") == "http.request":
            chunk = message.get("body", b"")
            # Kept up to the limit (and a byte past it, to know it was cut), never the rest of an upload.
            if self.size <= details.MAX_BODY:
                self.chunks.append(chunk[: details.MAX_BODY + 1 - self.size])
            self.size += len(chunk)
            self.complete = not message.get("more_body")
        return message

    def attrs(self, content_type: str) -> dict[str, Any]:
        data = b"".join(self.chunks)
        whole = self.complete and self.size <= details.MAX_BODY
        return details.body(content_type, data, complete=whole)


async def _read_body(receive: Receive) -> tuple[bytes, bool]:
    """A page's traces from the browser, sent on to the agent: read whole up to the limit, and whether it was over."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > MAX_BODY:
            return b"", True
        if not message.get("more_body"):
            return body, False


class _Responder:
    """Holds the response's start until the body is complete, then adds tracepanel's headers (and the widget)."""

    def __init__(self, scope: Scope, send: Send, trace: Trace, config: conf.Config) -> None:
        self.scope = scope
        self.downstream = send
        self.trace = trace
        self.config = config
        # Now, before the app runs: a Mount rewrites the scope's root_path on its way to the route.
        self.origin = _origin(scope)
        self.api = _api(scope, config.prefix)
        self.start: Message | None = None
        self.body: list[bytes] = []
        # Something already went to the client: an error can no longer get a response of its own.
        self.sent = False

    async def send(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.start = dict(message)
            self.trace.attrs["http.status_code"] = int(message["status"])
            if self.config.capture_request:
                pairs = [(key.decode("latin-1"), value.decode("latin-1")) for key, value in message.get("headers", [])]
                self.trace.attrs.update(details.response_headers(pairs))
            if int(message["status"]) >= 500:  # noqa: PLR2004
                self.trace.status = "error"
            return
        if kind != "http.response.body" or self.start is None:
            await self.downstream(message)
            return
        self.body.append(message.get("body", b""))
        if not message.get("more_body"):
            await self._flush()
            return
        if len(self.body) == 1 and not self._held():
            self._htmx(None)
            # Streaming: send what there is, with the id only (the timing isn't known yet).
            _set_header(self.start, "Tracepanel-Id", self.trace.id)
            self.sent = True
            await self.downstream(self.start)
            self.start = None
            await self.downstream(message)

    def _held(self) -> bool:
        """Whether a body in several messages is held until its end: its size is declared, and small enough.

        Otherwise it is a stream; an HTML page streamed so goes without the widget, and the terminal says why once.
        """
        if self.start is None:
            return False
        headers = _headers(self.start)
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) <= MAX_HELD:
            return True
        if self.config.widget and "text/html" in headers.get("content-type", ""):
            reason = "has no Content-Length" if not length.isdigit() else f"is over {MAX_HELD >> 20} MB"
            terminal.notice(f"{self.trace.name}: an HTML page streamed (it {reason}), so without the widget")
        return False

    def _htmx(self, body: bytes | None) -> None:
        if self.start is not None:
            status, headers = int(self.start["status"]), _headers(self.start)
            self.trace.attrs.update(htmx.response_attrs(self.trace.attrs, status, headers, body))

    async def error(self) -> None:
        """The plain 500 Starlette sends, with tracepanel's headers; nothing if the response already started."""
        if self.sent:
            return
        self.start = {
            "type": "http.response.start",
            "status": 500,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
        self.body = [b"Internal Server Error"]
        await self._flush()

    async def _flush(self) -> None:
        start = self.start
        if start is None:
            return
        body = b"".join(self.body)
        self._htmx(body)
        trace, config = self.trace, self.config
        _name(trace, self.scope, str(self.scope.get("method", "GET")))
        method = str(self.scope.get("method", "GET"))
        # complete() analyses the trace once it ends; the badge needs the issue count now.
        timing = widget.Timing.live(trace)
        requested = _headers({"headers": self.scope.get("headers", [])})
        page = widget.Page(method, int(start["status"]), self.origin, self.api, timing, requested=requested)
        headers = [(key.decode("latin-1"), value.decode("latin-1")) for key, value in start.get("headers", [])]
        decorated = widget.decorate(trace, page, headers, body, config)
        body = body if decorated is None else decorated
        start["headers"] = [(key.encode("latin-1"), value.encode("latin-1")) for key, value in headers]
        self.sent = True
        await self.downstream(start)
        await self.downstream({"type": "http.response.body", "body": body})
        self.start = None
