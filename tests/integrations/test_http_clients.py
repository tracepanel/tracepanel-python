import asyncio
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from dirty_equals import IsInt, IsPartialDict, IsStr
from jsonschema import Draft202012Validator

from tracepanel._compat import override
from tracepanel.core import w3c
from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.integrations import http_clients
from tracepanel.storage.transport import complete

http_clients.install()

ORIGIN = IsStr(regex=r"tests/integrations/test_http_clients\.py:\d+ in work")
# The traceparent each request reached the server with, in order.
received: list[str | None] = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        received.append(self.headers.get("traceparent"))
        path = self.path.split("?", 1)[0]
        if path == "/go":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = 200 if path == "/ok" else 404
        body = b"ok" if status == 200 else b"missing"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.do_GET()

    @override
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 (http.server's signature)
        pass


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def traced(work: Callable[[], object]) -> Trace:
    with start_trace("http", "GET /") as trace:
        work()
    return complete(trace)


def calls(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "http.client"]


def summary(trace: Trace) -> list[tuple[str, str, object]]:
    return [(span.name, span.status, span.attrs.get("http.response.status_code")) for span in calls(trace)]


# ---------- urllib3, and requests on it ----------


def test_a_requests_call_is_one_span(server: str, validator: Draft202012Validator) -> None:
    requests = pytest.importorskip("requests")

    def work() -> None:
        requests.get(f"{server}/ok", timeout=5)

    trace = traced(work)

    validator.validate(trace.to_dict())

    assert summary(trace) == [("GET 127.0.0.1/ok", "ok", 200)]
    assert calls(trace)[0].attrs == {
        "http.request.method": "GET",
        "url.full": f"{server}/ok",
        "url.scheme": "http",
        "server.address": "127.0.0.1",
        "server.port": IsInt(),
        "http.response.status_code": 200,
        "code.origin": ORIGIN,
        "w3c.span_id": w3c.span_id(calls(trace)[0].id),
    }


def carried(trace: Trace) -> list[str]:
    """The traceparent each of the trace's calls should have carried."""
    return [f"00-{trace.attrs['w3c.trace_id']}-{span.attrs['w3c.span_id']}-01" for span in calls(trace)]


def test_every_client_carries_the_trace_on(server: str) -> None:
    requests, urllib3, httpx = (pytest.importorskip(name) for name in ("requests", "urllib3", "httpx"))
    aiohttp = pytest.importorskip("aiohttp")

    async def asynchronous() -> None:
        async with httpx.AsyncClient() as client:
            await client.get(f"{server}/ok")
        async with aiohttp.ClientSession() as session, session.get(f"{server}/ok") as response:
            await response.read()

    def work() -> None:
        requests.get(f"{server}/ok", timeout=5)
        urllib3.PoolManager().request("GET", f"{server}/ok")
        httpx.get(f"{server}/ok")
        asyncio.run(asynchronous())

    received.clear()
    trace = traced(work)

    assert len(calls(trace)) == 5
    assert received == carried(trace)
    # The W3C trace is this trace's own: its id's 128 bits.
    assert trace.attrs["w3c.trace_id"] == w3c.trace_id(Trace(id=trace.id, kind="http", name="", start=0))


def test_a_trace_called_by_another_app_carries_that_one_on(server: str) -> None:
    requests = pytest.importorskip("requests")
    incoming = w3c.incoming("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")

    received.clear()
    with start_trace("http", "GET /", incoming) as trace:
        requests.get(f"{server}/ok", timeout=5)

    assert received == [f"00-4bf92f3577b34da6a3ce929d0e0e4736-{calls(trace)[0].attrs['w3c.span_id']}-01"]


def test_a_request_with_its_own_traceparent_keeps_it(server: str) -> None:
    requests = pytest.importorskip("requests")
    own = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    received.clear()
    traced(lambda: requests.get(f"{server}/ok", headers={"TraceParent": own}, timeout=5))
    # Outside a trace: nothing added.
    requests.get(f"{server}/ok", timeout=5)

    assert received == [own, None]


def test_an_error_status_and_a_redirect(server: str) -> None:
    requests = pytest.importorskip("requests")

    def work() -> None:
        requests.get(f"{server}/missing", timeout=5)
        requests.post(f"{server}/go", timeout=5)

    # requests follows the redirect itself: two requests on the wire, two spans.
    assert summary(traced(work)) == [
        ("GET 127.0.0.1/missing", "error", 404),
        ("POST 127.0.0.1/go", "ok", 302),
        ("GET 127.0.0.1/ok", "ok", 200),
    ]


def test_urllib3_following_a_redirect_itself_is_one_call_per_hop(server: str) -> None:
    urllib3 = pytest.importorskip("urllib3")

    def work() -> None:
        urllib3.request("GET", f"{server}/go")
        with urllib3.HTTPConnectionPool("127.0.0.1", int(server.rsplit(":", 1)[1])) as pool:
            # The pool follows the redirect inside its own urlopen(): the call the app made, one span.
            pool.urlopen("GET", "/go", redirect=True)

    assert summary(traced(work)) == [
        ("GET 127.0.0.1/go", "ok", 302),
        ("GET 127.0.0.1/ok", "ok", 200),
        ("GET 127.0.0.1/go", "ok", 200),
    ]


def test_a_failed_connection_is_an_error() -> None:
    requests = pytest.importorskip("requests")

    def work() -> None:
        with pytest.raises(requests.ConnectionError):
            requests.get("http://127.0.0.1:9/ok", timeout=2)

    span = calls(traced(work))[0]

    assert (span.status, span.attrs.get("error.type")) == ("error", IsStr())


def test_credentials_and_sensitive_query_values_are_left_out(server: str) -> None:
    requests = pytest.importorskip("requests")
    url = server.replace("http://", "http://user:pw@") + "/ok?page=2&api_key=abc&token=xyz"

    span = calls(traced(lambda: requests.get(url, timeout=5)))[0]

    assert span.attrs["url.full"] == f"{server}/ok?page=2&api_key=********&token=********"


# ---------- httpx, httpx2, aiohttp ----------


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_httpx_sync_and_async(server: str, module: str) -> None:
    httpx = pytest.importorskip(module)

    async def call_async() -> None:
        async with httpx.AsyncClient() as client:
            await client.get(f"{server}/missing")

    def work() -> None:
        with httpx.Client() as client:
            client.get(f"{server}/ok", params={"q": "x"})
        asyncio.run(call_async())

    trace = traced(work)

    assert summary(trace) == [("GET 127.0.0.1/ok", "ok", 200), ("GET 127.0.0.1/missing", "error", 404)]
    assert calls(trace)[0].attrs == IsPartialDict({"url.full": f"{server}/ok?q=x", "code.origin": ORIGIN})


def test_an_in_process_test_client_is_not_a_call() -> None:
    pytest.importorskip("starlette")
    from starlette.applications import Starlette  # noqa: PLC0415
    from starlette.responses import PlainTextResponse  # noqa: PLC0415
    from starlette.routing import Route  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    def home(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", home)])

    assert summary(traced(lambda: TestClient(app).get("/"))) == []


def test_aiohttp(server: str) -> None:
    aiohttp = pytest.importorskip("aiohttp")

    async def work() -> None:
        async with aiohttp.ClientSession(base_url=server) as session, session.get("/ok") as response:
            await response.read()

    trace = traced(lambda: asyncio.run(work()))

    assert summary(trace) == [("GET 127.0.0.1/ok", "ok", 200)]
    assert calls(trace)[0].attrs == IsPartialDict({"url.full": f"{server}/ok", "code.origin": ORIGIN})


# ---------- who called ----------


def calling_from(module: str) -> Callable[[str], Any]:
    namespace: dict[str, Any] = {"__name__": module}
    exec("import requests\ndef get(url):\n    return requests.get(url, timeout=5)\n", namespace)  # noqa: S102
    return namespace["get"]


@pytest.mark.parametrize("module", ["tracepanel.storage.agent", "opentelemetry.exporter.otlp.proto.http"])
def test_tracepanel_s_and_opentelemetry_s_own_calls_are_left_out(server: str, module: str) -> None:
    pytest.importorskip("requests")

    assert summary(traced(lambda: calling_from(module)(f"{server}/ok"))) == []


def test_outside_a_trace_it_only_calls(server: str) -> None:
    requests = pytest.importorskip("requests")

    assert requests.get(f"{server}/ok", timeout=5).text == "ok"
