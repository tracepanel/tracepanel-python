"""WSGI apps: any of them at the request level, Flask and Falcon in full through their own modules.

`app = tracepanel.wsgi.install(app)` wraps any WSGI app (Bottle, Pyramid, a bare callable): use what it returns. It is
on only for an app in debug mode (an app with `debug = True`) or with TRACEPANEL_ENABLED=1.

Requests become traces named after their path (a framework module names them after the route), with their status
and errors; a complete response gets the widget's headers and, for an HTML page, its script; a streamed one gets
Tracepanel-Id only. The console is served under `/__tracepanel__/`. Settings are keyword arguments (the same names as
Django's TRACEPANEL dict); TRACEPANEL_* variables override them.
"""

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from tracepanel import _setup
from tracepanel.console import widget
from tracepanel.core import conf, errors
from tracepanel.core.context import current_trace, start_trace
from tracepanel.core.model import Trace
from tracepanel.integrations import htmx
from tracepanel.storage.transport import complete

StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]
Headers = list[tuple[str, str]]

# Where a request that failed past the app (into a debugger) leaves its trace, for the debugger's page.
TRACE_KEY = "tracepanel.trace_id"
URL_KEY = "tracepanel.console_url"

__all__ = ["TracepanelMiddleware", "install"]


def host(environ: dict[str, Any]) -> str:
    name = environ.get("HTTP_HOST") or f"{environ.get('SERVER_NAME')}:{environ.get('SERVER_PORT')}"
    return f"{environ.get('wsgi.url_scheme', 'http')}://{name}"


class _Response:
    """What the app passed to start_response, held until tracepanel has added its headers."""

    def __init__(self) -> None:
        self.status: str | None = None
        self.headers: Headers = []
        self.exc_info: Any = None
        self.written: list[bytes] = []

    def start_response(self, status: str, headers: Headers, exc_info: Any = None) -> Callable[[bytes], None]:  # noqa: ANN401
        self.status, self.headers, self.exc_info = status, list(headers), exc_info
        # The legacy write() callable: what it writes comes before the body.
        return self.written.append

    @property
    def code(self) -> int:
        return int(str(self.status).split(" ", 1)[0])


class TracepanelMiddleware:
    """Traces the wrapped app's requests and serves the console under the prefix."""

    def __init__(self, app: WSGIApp) -> None:
        self.app = app
        self.first = True

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        if self.first:
            # A framework may turn debug on after the app (and tracepanel) is set up: read the settings again.
            self.first = False
            conf.reset()
        config = conf.get_config()
        path = str(environ.get("PATH_INFO", "/"))
        if path.startswith(config.prefix):
            from tracepanel.console import server  # noqa: PLC0415

            return server.wsgi({**environ, "PATH_INFO": path.removeprefix(config.prefix)}, start_response)
        if not config.enabled or conf.matches(path, config.ignore_paths) or current_trace() is not None:
            # Already traced by an app this one is mounted in: its framework module adds to that trace.
            return self.app(environ, start_response)
        method = str(environ.get("REQUEST_METHOD", "GET"))
        response = _Response()
        attrs = {"http.method": method, "http.path": path, **htmx.request_attrs(_request_headers(environ))}
        with start_trace("http", f"{method} {path}", attrs) as trace:
            try:
                result = self.app(environ, response.start_response)
                chunks = iter(result)
                # A generator app calls start_response with its first chunk.
                first: list[bytes] = [] if response.status is not None else list(_first(chunks))
                streamed = widget.header(response.headers, "Content-Length") is None and not isinstance(
                    result, (list, tuple)
                )
                body = None if streamed else b"".join([*response.written, *first, *chunks])
            except Exception as exc:
                # Through to a debugger (Werkzeug's): no response came back. Its page gets the trace's headers.
                if not trace.errors:
                    errors.capture(exc, handled=False)
                trace.status = "error"
                trace.attrs["http.status_code"] = 500
                environ[TRACE_KEY] = trace.id
                environ[URL_KEY] = f"{host(environ)}{_api(environ, config)}"
                complete(trace)
                raise
            trace.attrs["http.status_code"] = response.code
            if response.code >= 500:  # noqa: PLR2004
                trace.status = "error"
            headers = {name.lower(): value for name, value in response.headers}
            trace.attrs.update(htmx.response_attrs(trace.attrs, response.code, headers, body))
            widget.set_header(response.headers, "Tracepanel-Id", trace.id)
            if body is not None:
                body = _decorate(environ, response, trace, config, body)
                _close(result)
        complete(trace)
        start_response(response.status, response.headers, response.exc_info)
        if body is not None:
            return [body]
        # Streamed: the body goes out as the app makes it; the timing isn't known in advance.
        return _Streamed([*response.written, *first], chunks, result)


def _request_headers(environ: dict[str, Any]) -> dict[str, str]:
    """HTTP_HX_REQUEST -> hx-request: the request's headers by lower-case name."""
    return {key[5:].lower().replace("_", "-"): str(value) for key, value in environ.items() if key.startswith("HTTP_")}


def _first(chunks: Iterator[bytes]) -> Iterator[bytes]:
    for chunk in chunks:
        yield chunk
        return


def _close(result: Iterable[bytes]) -> None:
    close = getattr(result, "close", None)
    if close is not None:
        close()


class _Streamed:
    def __init__(self, head: list[bytes], chunks: Iterator[bytes], result: Iterable[bytes]) -> None:
        self.head, self.chunks, self.result = head, chunks, result

    def __iter__(self) -> Iterator[bytes]:
        yield from self.head
        yield from self.chunks

    def close(self) -> None:
        _close(self.result)


def _api(environ: dict[str, Any], config: conf.Config) -> str:
    return str(environ.get("SCRIPT_NAME", "")).rstrip("/") + config.prefix


def _decorate(environ: dict[str, Any], response: _Response, trace: Trace, config: conf.Config, body: bytes) -> bytes:
    method = str(environ.get("REQUEST_METHOD", "GET"))
    # The trace ends after the response is made: its time so far, its issues now.
    timing = widget.Timing.live(trace, config)
    page = widget.Page(method, response.code, host(environ), _api(environ, config), timing)
    decorated = widget.decorate(trace, page, response.headers, body, config)
    return body if decorated is None else decorated


def install(app: WSGIApp, **settings: object) -> WSGIApp:
    """Trace the app's requests; returns the app to serve (the app itself when tracepanel is off)."""
    values = {"enabled": bool(getattr(app, "debug", False)), **settings}
    if not _setup.enable(lambda: values):
        return app
    _setup.started("WSGI app")
    return TracepanelMiddleware(app)
