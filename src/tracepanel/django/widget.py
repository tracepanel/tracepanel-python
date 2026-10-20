"""The widget on Django's responses: badge headers on every traced response, the script in HTML pages."""

from collections.abc import AsyncIterator, Iterator

from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.http.response import HttpResponseBase

from tracepanel.console import widget
from tracepanel.core.conf import Config
from tracepanel.core.model import Trace


def decorate(request: HttpRequest, response: HttpResponseBase, trace: Trace, config: Config) -> None:
    api = request.META.get("SCRIPT_NAME", "").rstrip("/") + config.prefix
    origin = request.build_absolute_uri("/").rstrip("/")
    timing = widget.Timing.of(trace)
    requested = {name.lower(): value for name, value in request.headers.items()}
    page = widget.Page(
        request.method or "GET",
        response.status_code,
        origin,
        api,
        timing,
        response.charset,
        requested,
        cookies_in_headers=False,
    )
    if (redirect := widget.redirected(trace, page)) is not None:
        response.set_cookie(widget.REDIRECTS, redirect, max_age=30, path="/", samesite="Lax")
    headers = list(response.items())
    if isinstance(response, StreamingHttpResponse):
        # A streamed page gets the script as its <head> passes; a file response declares its size and is left as is.
        script = widget.stream(trace, page, headers, config)
        _headers(response, headers)
        if script is not None:
            _stream_through(response, script)
        return
    body = response.content if isinstance(response, HttpResponse) else None
    decorated = widget.decorate(trace, page, headers, body, config)
    _headers(response, headers)
    if isinstance(response, HttpResponse) and decorated is not None and decorated is not body:
        response.content = decorated


def _headers(response: HttpResponseBase, headers: widget.Headers) -> None:
    for name, value in headers:
        if response.get(name) != value:
            response[name] = value


def _stream_through(response: StreamingHttpResponse, script: widget.Stream) -> None:
    """The response's pieces through the script, sync or async as the response streams them."""
    if response.is_async:
        response.streaming_content = _async_through(response.streaming_content, script)  # type: ignore[arg-type]
    else:
        response.streaming_content = _sync_through(response.streaming_content, script)  # type: ignore[arg-type]


def _sync_through(pieces: Iterator[bytes], script: widget.Stream) -> Iterator[bytes]:
    for piece in pieces:
        if passed := script.feed(piece):
            yield passed
    if rest := script.end():
        yield rest


async def _async_through(pieces: AsyncIterator[bytes], script: widget.Stream) -> AsyncIterator[bytes]:
    async for piece in pieces:
        if passed := script.feed(piece):
            yield passed
    if rest := script.end():
        yield rest
