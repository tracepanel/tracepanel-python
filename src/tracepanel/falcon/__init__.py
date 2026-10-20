"""Falcon (WSGI and ASGI apps): tracepanel.wsgi or tracepanel.asgi, plus what only Falcon knows.

`tracepanel run gunicorn app:app` (or `tracepanel run uvicorn app:app` for a falcon.asgi.App) needs no code: the app is
set up as it is created. In code, `tracepanel.falcon.install(app)` does the same, in place (the app stays the object to
serve); it is on only with TRACEPANEL_ENABLED=1 or `install(app, enabled=True)`, as Falcon has no debug mode of its own.

tracepanel.wsgi (falcon.App) or tracepanel.asgi (falcon.asgi.App) makes requests traces (status, the widget's headers
and script, the console under `/__tracepanel__/`); this module names them after the route's URI template
(`GET /posts/{post_id}`), or after the GraphQL operation a responder ran with Strawberry, runs the responder in a `view`
span named after it (`app.resources.PostResource.on_get`) and records errors Falcon turns into a 500. Falcon has no
session or user of its own: none is recorded.
"""

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Unpack

from tracepanel import _setup
from tracepanel.asgi import TracepanelMiddleware as ASGIMiddleware
from tracepanel.asgi.middleware import Receive, Scope, Send
from tracepanel.core import errors, patching
from tracepanel.core.conf import Settings
from tracepanel.core.context import current_trace, start_span
from tracepanel.core.patching import patch, patched
from tracepanel.wsgi import TracepanelMiddleware

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    import falcon
    import falcon.asgi

    from tracepanel.core.model import Span

StartResponse = Callable[..., Any]
# Falcon's App has __slots__: the middleware of each installed app is kept here (apps live as long as the process).
_traced: dict[int, tuple[object, TracepanelMiddleware]] = {}
_traced_asgi: dict[int, tuple[object, ASGIMiddleware]] = {}


def _responder(req: "falcon.Request", resource: object) -> str:
    """The responder Falcon picked (`PostResource.on_get_fixed` for a route added with suffix="fixed")."""
    responder = getattr(req.context, "tracepanel_responder", None)
    target = getattr(responder, "__func__", responder)
    if target is not None and hasattr(target, "__qualname__"):
        return f"{target.__module__}.{target.__qualname__}"
    cls = type(resource)
    return f"{cls.__module__}.{cls.__qualname__}.on_{req.method.lower()}"


def _enter(req: "falcon.Request", resource: object) -> None:
    """The route names the trace, the responder runs inside a `view` span."""
    trace = current_trace()
    if trace is None or resource is None:
        return
    template = req.uri_template
    if template:
        trace.attrs["http.route"] = template
        trace.name = f"{req.method} {template}"
    name = _responder(req, resource)
    trace.attrs["code.view"] = name
    span = start_span("view", name, {"code.function": name})
    span.__enter__()
    req.context.tracepanel_view = span


def _leave(req: "falcon.Request") -> None:
    """The `view` span ends; a request that ran a GraphQL operation is named after it, as Django's and Flask's are."""
    span: AbstractContextManager[Span | None] | None = getattr(req.context, "tracepanel_view", None)
    if span is not None:
        span.__exit__(None, None, None)
    trace = current_trace()
    operation = trace.attrs.get("graphql.operation") if trace is not None else None
    if trace is not None and isinstance(operation, str):
        trace.name = f"GraphQL {operation}"


class _Views:
    """Falcon middleware for both kinds of app: Falcon calls the `*_async` methods in an ASGI app."""

    def process_resource(self, req: "falcon.Request", resp: object, resource: object, params: object) -> None:
        del resp, params
        _enter(req, resource)

    def process_response(self, req: "falcon.Request", resp: object, resource: object, succeeded: bool) -> None:  # noqa: FBT001 (Falcon's)
        del resp, resource, succeeded
        _leave(req)

    async def process_resource_async(
        self, req: "falcon.Request", resp: object, resource: object, params: object
    ) -> None:
        del resp, params
        _enter(req, resource)

    async def process_response_async(
        self,
        req: "falcon.Request",
        resp: object,
        resource: object,
        succeeded: bool,  # noqa: FBT001 (Falcon's)
    ) -> None:
        del resp, resource, succeeded
        _leave(req)


def _record(app: Any, ex: Exception) -> None:  # noqa: ANN401 (Falcon's App)
    """An exception Falcon's default handler answers (with a 500) is the trace's error."""
    trace = current_trace()
    handler = app._find_error_handler(ex)  # noqa: SLF001
    default = getattr(type(app), "_python_error_handler", None)
    if trace is not None and default is not None and getattr(handler, "__func__", handler) is default:
        errors.capture(ex, handled=False)
        trace.status = "error"


def _errors(app_class: type) -> None:
    """Errors Falcon turns into a 500 (its default handler for Exception) are the trace's errors.

    One the app handles itself (`app.add_error_handler(ValueError, ...)`) is an answer, as an HTTPError is.
    """
    if patched(app_class, "_handle_exception"):
        return
    original = app_class._handle_exception  # noqa: SLF001

    def handle(self: Any, req: Any, resp: Any, ex: Exception, params: Any) -> bool:  # noqa: ANN401 (Falcon's)
        _record(self, ex)
        handled: bool = original(self, req, resp, ex, params)
        return handled

    patch(app_class, "_handle_exception", handle)


def _errors_async(app_class: type) -> None:
    """The same for an ASGI app, whose _handle_exception is a coroutine (and handles WebSocket errors too)."""
    if patched(app_class, "_handle_exception"):
        return
    original = app_class._handle_exception  # noqa: SLF001

    async def handle(self: Any, req: Any, resp: Any, ex: Exception, params: Any, **kwargs: Any) -> bool:  # noqa: ANN401 (Falcon's)
        _record(self, ex)
        handled: bool = await original(self, req, resp, ex, params, **kwargs)
        return handled

    patch(app_class, "_handle_exception", handle)


def _responders(app_class: type) -> None:
    """Remember the responder Falcon routes the request to: its name (suffix included) names the view span.

    falcon.asgi.App inherits it: patching falcon.App covers both kinds of app.
    """
    if patched(app_class, "_get_responder"):
        return
    original = app_class._get_responder  # noqa: SLF001

    def get_responder(self: Any, req: Any) -> Any:  # noqa: ANN401 (Falcon's)
        found = original(self, req)
        req.context.tracepanel_responder = found[0]
        return found

    patch(app_class, "_get_responder", get_responder)


def _call(app_class: type) -> Callable[..., Iterable[bytes]]:
    """An installed app's requests go through tracepanel.wsgi: Falcon's App is the WSGI callable, served as is.

    Returns Falcon's own __call__, past the patch.
    """
    original: Callable[..., Iterable[bytes]] = patching.original(app_class, "__call__")
    if patched(app_class, "__call__"):
        return original

    def call(self: Any, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:  # noqa: ANN401
        traced = _traced.get(id(self))
        if traced is None:
            return original(self, environ, start_response)
        return traced[1](environ, start_response)

    patch(app_class, "__call__", call)
    return original


def _call_async(app_class: type) -> Callable[..., Awaitable[None]]:
    """An installed ASGI app's requests go through tracepanel.asgi, the app served as is.

    Returns Falcon's own __call__, past the patch.
    """
    original: Callable[..., Awaitable[None]] = patching.original(app_class, "__call__")
    if patched(app_class, "__call__"):
        return original

    async def call(self: Any, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: ANN401
        traced = _traced_asgi.get(id(self))
        if traced is None:
            await original(self, scope, receive, send)
            return
        await traced[1](scope, receive, send)

    patch(app_class, "__call__", call)
    return original


def _install_asgi(app: "falcon.asgi.App[Any, Any]") -> None:
    import falcon.asgi  # noqa: PLC0415

    _errors_async(falcon.asgi.App)
    falcon_call = _call_async(falcon.asgi.App)

    async def plain(scope: Scope, receive: Receive, send: Send) -> None:
        await falcon_call(app, scope, receive, send)

    _traced_asgi[id(app)] = (app, ASGIMiddleware(plain))


def _install_wsgi(app: "falcon.App[Any, Any]") -> None:
    import falcon  # noqa: PLC0415

    _errors(falcon.App)
    falcon_call = _call(falcon.App)

    def plain(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        return falcon_call(app, environ, start_response)

    _traced[id(app)] = (app, TracepanelMiddleware(plain))


def install(app: "falcon.App[Any, Any] | falcon.asgi.App[Any, Any]", **settings: Unpack[Settings]) -> None:
    import falcon  # noqa: PLC0415
    import falcon.asgi  # noqa: PLC0415

    if id(app) in _traced or id(app) in _traced_asgi:
        return
    if not _setup.enable(lambda: {"enabled": False, **settings}):
        return
    app.add_middleware(_Views())
    _responders(falcon.App)
    if isinstance(app, falcon.asgi.App):
        _install_asgi(app)
    else:
        _install_wsgi(app)
    _setup.started("Falcon")
