"""WSGI apps: any of them at the request level, Flask and Falcon in full through their own modules.

`app = tracepanel.wsgi.install(app)` wraps any WSGI app (Bottle, Pyramid, a bare callable): use what it returns. It is
on only for an app in debug mode (an app with `debug = True`) or with TRACEPANEL_ENABLED=1.

Requests become traces named after their path (a framework module names them after the route), with their status
and errors; a response gets the widget's headers and, for an HTML page, its script: a complete one at once, a
streamed one as its pieces pass. The console is served under `/__tracepanel__/`. Settings are keyword arguments (the
same names as Django's TRACEPANEL dict); TRACEPANEL_* variables override them.
"""

import http
from collections.abc import Callable, Iterable, Iterator
from io import BytesIO
from typing import IO, Any, Unpack
from urllib.parse import parse_qsl

from tracepanel import _setup
from tracepanel.console import app as console
from tracepanel.console import widget
from tracepanel.console.messages import MAX_BODY, is_local
from tracepanel.core import conf, errors, profile, w3c
from tracepanel.core import request as details
from tracepanel.core.conf import Settings
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


def _answered(
    trace: Trace, response: _Response, body: bytes | None, reader: "_BodyReader | _NoBody | None", content_type: str
) -> None:
    """Record how the app answered: its status, htmx's headers, what the request carried; mark the trace."""
    trace.attrs["http.status_code"] = response.code
    if response.code >= 500:  # noqa: PLR2004
        trace.status = "error"
    headers = {name.lower(): value for name, value in response.headers}
    trace.attrs.update(htmx.response_attrs(trace.attrs, response.code, headers, body))
    if reader is not None:
        trace.attrs.update(reader.attrs(content_type, response.headers))
    widget.set_header(response.headers, "Tracepanel-Id", trace.id)


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
            return _console(environ, path.removeprefix(config.prefix), start_response)
        if not config.enabled or conf.matches(path, config.ignore_paths) or current_trace() is not None:
            # Already traced by an app this one is mounted in: its framework module adds to that trace.
            return self.app(environ, start_response)
        method = str(environ.get("REQUEST_METHOD", "GET"))
        response = _Response()
        headers = _request_headers(environ)
        attrs = {
            "http.method": method,
            "http.path": path,
            **htmx.request_attrs(headers),
            # Called by another app: the W3C trace its request carried.
            **w3c.incoming(headers.get(w3c.HEADER)),
        }
        reader = _capture(environ, headers, attrs) if config.capture_request else None
        asked = profile.requested(headers.get(profile.HEADER), headers.get("cookie"))
        script: widget.Stream | None = None
        with start_trace("http", f"{method} {path}", attrs, profile=asked) as trace:
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
            _answered(trace, response, body, reader, str(environ.get("CONTENT_TYPE", "")))
            if body is not None:
                body = _decorate(environ, response, trace, config, body)
                _close(result)
            else:
                script = widget.stream(trace, _page(environ, response, trace, config), response.headers, config)
        complete(trace)
        start_response(response.status, response.headers, response.exc_info)
        if body is not None:
            return [body]
        # Streamed: the body goes out as the app makes it; the timing isn't known in advance.
        return _Streamed([*response.written, *first], chunks, result, script)


def _request_headers(environ: dict[str, Any]) -> dict[str, str]:
    """HTTP_HX_REQUEST -> hx-request: the request's headers by lower-case name."""
    return {key[5:].lower().replace("_", "-"): str(value) for key, value in environ.items() if key.startswith("HTTP_")}


def _capture(environ: dict[str, Any], headers: dict[str, str], attrs: dict[str, Any]) -> "_BodyReader | _NoBody":
    """The request's headers, query and cookies into its attributes, and a reader keeping what the app reads."""
    # CONTENT_TYPE and CONTENT_LENGTH are the two headers WSGI doesn't prefix with HTTP_.
    unprefixed = [(name, str(environ[key])) for key, name in _UNPREFIXED if environ.get(key)]
    attrs.update(details.request_headers([*unprefixed, *headers.items()]))
    attrs.update(details.query(str(environ.get("QUERY_STRING", ""))))
    attrs.update(details.cookies(headers.get("cookie", "")))
    # A request without a body (most GETs) keeps wsgi.input as it is: nothing to read, nothing to wrap.
    length = str(environ.get("CONTENT_LENGTH") or "")
    chunked = "chunked" in headers.get("transfer-encoding", "").lower()
    return _BodyReader(environ) if chunked or (length.isdigit() and int(length) > 0) else _NO_BODY


class _NoBody:
    """What a request without a body records at its end: the response's headers."""

    def attrs(self, content_type: str, response_headers: Headers) -> dict[str, Any]:
        del content_type
        return details.response_headers(response_headers)


_NO_BODY = _NoBody()


_UNPREFIXED = (("CONTENT_TYPE", "content-type"), ("CONTENT_LENGTH", "content-length"))


class _BodyReader:
    """Stands in for wsgi.input, keeping the start of what the app reads: never more, never the rest of an upload."""

    def __init__(self, environ: dict[str, Any]) -> None:
        self.stream: IO[bytes] = environ.get("wsgi.input") or BytesIO()
        self.chunks: list[bytes] = []
        self.size = 0
        length = str(environ.get("CONTENT_LENGTH") or "")
        self.length = int(length) if length.isdigit() else 0
        environ["wsgi.input"] = self

    def _kept(self, data: bytes) -> bytes:
        if self.size <= details.MAX_BODY:
            self.chunks.append(data[: details.MAX_BODY + 1 - self.size])
        self.size += len(data)
        return data

    def read(self, size: int = -1) -> bytes:
        return self._kept(self.stream.read(size) if size is not None and size >= 0 else self.stream.read())

    def readline(self, size: int = -1) -> bytes:
        return self._kept(self.stream.readline(size))

    def readlines(self, hint: int = -1) -> list[bytes]:
        return [self._kept(line) for line in self.stream.readlines(hint)]

    def __iter__(self) -> Iterator[bytes]:
        return (self._kept(line) for line in self.stream)

    def attrs(self, content_type: str, response_headers: Headers) -> dict[str, Any]:
        """The response's headers, and the body as far as the app read it."""
        data = b"".join(self.chunks)
        whole = self.size <= details.MAX_BODY and (self.length == 0 or self.size >= self.length)
        return {**details.response_headers(response_headers), **details.body(content_type, data, complete=whole)}


def _first(chunks: Iterator[bytes]) -> Iterator[bytes]:
    for chunk in chunks:
        yield chunk
        return


def _close(result: Iterable[bytes]) -> None:
    close = getattr(result, "close", None)
    if close is not None:
        close()


class _Streamed:
    """A streamed body as the app makes it, through the widget's script when it is an HTML page."""

    def __init__(
        self, head: list[bytes], chunks: Iterator[bytes], result: Iterable[bytes], script: widget.Stream | None
    ) -> None:
        self.head, self.chunks, self.result, self.script = head, chunks, result, script

    def __iter__(self) -> Iterator[bytes]:
        script = self.script
        if script is None:
            yield from self.head
            yield from self.chunks
            return
        for piece in (*self.head, *self.chunks):
            passed = script.feed(piece)
            # An empty piece is a flush the app asked for: it goes out too, after what can go with it.
            if passed or not piece:
                yield passed
        if rest := script.end():
            yield rest

    def close(self) -> None:
        _close(self.result)


def _api(environ: dict[str, Any], config: conf.Config) -> str:
    return str(environ.get("SCRIPT_NAME", "")).rstrip("/") + config.prefix


def _page(environ: dict[str, Any], response: _Response, trace: Trace, config: conf.Config) -> widget.Page:
    method = str(environ.get("REQUEST_METHOD", "GET"))
    # The trace ends after the response is made: its time so far, its issues now.
    timing = widget.Timing.live(trace)
    return widget.Page(
        method, response.code, host(environ), _api(environ, config), timing, requested=_request_headers(environ)
    )


def _decorate(environ: dict[str, Any], response: _Response, trace: Trace, config: conf.Config, body: bytes) -> bytes:
    decorated = widget.decorate(trace, _page(environ, response, trace, config), response.headers, body, config)
    return body if decorated is None else decorated


def install(app: WSGIApp, **settings: Unpack[Settings]) -> WSGIApp:
    """Trace the app's requests; returns the app to serve (the app itself when tracepanel is off)."""
    values = {"enabled": bool(getattr(app, "debug", False)), **settings}
    if not _setup.enable(lambda: values):
        return app
    _setup.started("WSGI app")
    return TracepanelMiddleware(app)


def _console(environ: dict[str, Any], path: str, start_response: StartResponse) -> Iterable[bytes]:
    """The console under the prefix (the agent's, forwarded; EXPLAIN needs a framework, so not here)."""
    method = str(environ.get("REQUEST_METHOD", "GET"))
    length = int(environ.get("CONTENT_LENGTH") or 0) if method == "POST" else 0
    too_large = length > MAX_BODY
    readable = length if not too_large else 0
    request = console.Request(
        method=method,
        path=path.lstrip("/"),
        query=dict(parse_qsl(str(environ.get("QUERY_STRING", "")))),
        # A page's traces from the browser, sent on to the agent.
        body=environ["wsgi.input"].read(readable) if readable else b"",
        content_type=str(environ.get("CONTENT_TYPE", "")) if readable else "",
        local=is_local(str(environ.get("REMOTE_ADDR", ""))),
        too_large=too_large,
    )
    response = console.dispatch(request)
    status = f"{response.status} {http.HTTPStatus(response.status).phrase}"
    start_response(status, [("Content-Type", response.content_type), *response.headers.items()])
    return [response.body]
