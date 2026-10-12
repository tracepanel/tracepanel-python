"""Outgoing HTTP calls: every request the app makes as an `http.client` span.

- urllib3, and what is built on it: requests, botocore (boto3: S3 and the rest of AWS), most API SDKs.
- httpx and httpx2 (its fork Starlette's test client uses), sync and async: the transport that reaches the network,
  so a test client's in-process transport isn't a call.
- aiohttp's client.

A call is recorded where it leaves for the network, once: requests on urllib3 is one call, and a redirect urllib3
follows itself stays inside it (one PoolManager hop is one call). Who called decides, as for SQL
(tracepanel.core.callers): tracepanel's own calls and OpenTelemetry's exporters are left out.
"""

import importlib
import sys
import time
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tracepanel.core.callers import Callers
from tracepanel.core.context import current_trace, now_us, record_span
from tracepanel.core.frames import MASK, SENSITIVE, origin
from tracepanel.core.model import Attrs
from tracepanel.core.patching import patch

T = TypeVar("T")

CALLERS = Callers(
    own=__name__,
    libraries=("urllib3", "requests", "botocore", "boto3", "s3transfer", "httpx", "httpx2", "httpcore", "aiohttp"),
    layers=("tracepanel.", "opentelemetry."),
)
# A status from this one on makes the call an error, as OpenTelemetry has it for a client.
_ERROR_STATUS = 400


def safe_url(url: str) -> str:
    """The URL without its credentials, and with the values of sensitive query parameters masked."""
    parts = urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode([(key, MASK if SENSITIVE.search(key) else value) for key, value in pairs], safe="*")
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _attrs(method: str, url: str) -> Attrs:
    parts = urlsplit(url)
    attrs: Attrs = {
        "http.request.method": method.upper(),
        "url.full": safe_url(url),
        "url.scheme": parts.scheme or "http",
        "server.address": parts.hostname,
        "server.port": parts.port or (443 if parts.scheme == "https" else 80),
    }
    return attrs


def _name(method: str, url: str) -> str:
    parts = urlsplit(url)
    return f"{method.upper()} {parts.hostname or ''}{parts.path or '/'}"


def _finish(method: str, url: str, started: tuple[int, int], *, status_code: int | None, failed: str | None) -> None:
    """Record the call: `started` is its start (now_us) and perf_counter_ns() then."""
    start, counter = started
    attrs = _attrs(method, url)
    if status_code is not None:
        attrs["http.response.status_code"] = status_code
    if failed is not None:
        attrs["error.type"] = failed
    attrs["code.origin"] = origin()
    error = failed is not None or (status_code is not None and status_code >= _ERROR_STATUS)
    record_span(
        "http.client",
        _name(method, url),
        start=start,
        duration=(time.perf_counter_ns() - counter) // 1000,
        attrs=attrs,
        status="error" if error else "ok",
    )


def call(send: Callable[[], T], method: str, url: str, status: Callable[[T], int | None]) -> T:
    """Make the call, recorded when the app made it inside a trace."""
    if current_trace() is None or not CALLERS.app_called(sys._getframe(1)):  # noqa: SLF001 (who called)
        return send()
    start, counter = now_us(), time.perf_counter_ns()
    try:
        response = send()
    except Exception as exc:
        _finish(method, url, (start, counter), status_code=None, failed=type(exc).__name__)
        raise
    _finish(method, url, (start, counter), status_code=status(response), failed=None)
    return response


async def call_async(send: Callable[[], Awaitable[T]], method: str, url: str, status: Callable[[T], int | None]) -> T:
    if current_trace() is None or not CALLERS.app_called(sys._getframe(1)):  # noqa: SLF001
        return await send()
    start, counter = now_us(), time.perf_counter_ns()
    try:
        response = await send()
    except Exception as exc:
        _finish(method, url, (start, counter), status_code=None, failed=type(exc).__name__)
        raise
    _finish(method, url, (start, counter), status_code=status(response), failed=None)
    return response


# ---------- urllib3 ----------


def _urllib3(urllib3: ModuleType) -> None:
    pool = urllib3.connectionpool.HTTPConnectionPool
    urlopen = pool.urlopen

    def traced(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (urllib3's signature)
        # The pool's own URLs are paths; an absolute one goes through a proxy.
        full = url if "://" in url else f"{self.scheme}://{self.host}:{self.port}{url}"
        return call(lambda: urlopen(self, method, url, *args, **kwargs), method, full, lambda r: r.status)

    patch(pool, "urlopen", traced)


# ---------- httpx, httpx2 ----------


def _httpx(httpx: ModuleType) -> None:
    sync, asynchronous = httpx.HTTPTransport, httpx.AsyncHTTPTransport
    handle, handle_async = sync.handle_request, asynchronous.handle_async_request

    def traced(self: Any, request: Any) -> Any:  # noqa: ANN401 (an httpx Request and Response)
        return call(lambda: handle(self, request), request.method, str(request.url), lambda r: r.status_code)

    async def traced_async(self: Any, request: Any) -> Any:  # noqa: ANN401
        return await call_async(
            lambda: handle_async(self, request), request.method, str(request.url), lambda r: r.status_code
        )

    patch(sync, "handle_request", traced)
    patch(asynchronous, "handle_async_request", traced_async)


# ---------- aiohttp ----------


def _aiohttp(aiohttp: ModuleType) -> None:
    yarl = importlib.import_module("yarl")  # aiohttp's URLs
    session = aiohttp.ClientSession
    request = session._request  # noqa: SLF001 (what every request method of the session goes through)

    async def traced(self: Any, method: str, str_or_url: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        base = getattr(self, "_base_url", None)
        url = str(base.join(yarl.URL(str(str_or_url)))) if base else str(str_or_url)
        return await call_async(
            lambda: request(self, method, str_or_url, *args, **kwargs), method, url, lambda r: r.status
        )

    patch(session, "_request", traced)


def _optional(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def install() -> None:
    """Wrap the HTTP clients that are installed."""
    if (urllib3 := _optional("urllib3")) is not None:
        importlib.import_module("urllib3.connectionpool")
        _urllib3(urllib3)
    for name in ("httpx", "httpx2"):
        if (httpx := _optional(name)) is not None:
            _httpx(httpx)
    if (aiohttp := _optional("aiohttp")) is not None:
        _aiohttp(aiohttp)
