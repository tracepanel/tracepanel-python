import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tests.agentstore import issues
from tracepanel._compat import override
from tracepanel.core.context import on_event_loop, start_trace
from tracepanel.core.model import Trace
from tracepanel.integrations import http_clients
from tracepanel.integrations.dbapi import sqlite as dbapi_sqlite
from tracepanel.storage.transport import complete

http_clients.install()
dbapi_sqlite.install()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    @override
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 (http.server's signature)
        pass


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def in_async_view(work: Callable[[], Awaitable[None]]) -> Trace:
    """`work` as an async view's: run on the event loop, inside a trace."""

    async def view() -> Trace:
        with start_trace("http", "GET /checkout") as trace:
            await work()
        return trace

    return complete(asyncio.run(view()))


def blocking(trace: Trace) -> list[tuple[str, bool]]:
    return [(span.kind, span.attrs.get("code.blocks_event_loop") is True) for span in trace.spans[1:]]


def test_only_a_running_event_loop_is_blocked() -> None:
    async def inside() -> bool:
        return on_event_loop()

    assert on_event_loop() is False
    assert asyncio.run(inside()) is True
    # A thread off the loop (sync_to_async, asyncio.to_thread) blocks nothing.
    assert asyncio.run(asyncio.to_thread(on_event_loop)) is False


def test_synchronous_calls_on_the_loop_are_marked(server: str) -> None:
    requests = pytest.importorskip("requests")
    httpx = pytest.importorskip("httpx")

    async def work() -> None:
        with sqlite3.connect(":memory:") as db:
            db.execute("SELECT 1")
        requests.get(f"{server}/", timeout=5)
        # Awaited: the loop serves others meanwhile.
        async with httpx.AsyncClient() as client:
            await client.get(f"{server}/")
        # In a thread: not the loop's.
        await asyncio.to_thread(requests.get, f"{server}/", timeout=5)

    assert blocking(in_async_view(work)) == [
        ("sql", True),
        ("http.client", True),
        ("http.client", False),
        ("http.client", False),
    ]


def test_a_sync_view_blocks_nothing(server: str) -> None:
    requests = pytest.importorskip("requests")

    with start_trace("http", "GET /checkout") as trace:
        requests.get(f"{server}/", timeout=5)

    assert blocking(complete(trace)) == [("http.client", False)]


def test_the_agent_finds_the_blocking_call(server: str) -> None:
    requests = pytest.importorskip("requests")

    async def work() -> None:
        for path in ("/rates", "/stock", "/tax"):
            requests.get(f"{server}{path}", timeout=5)

    trace = in_async_view(work)
    # The agent counts a place's blocking calls once they cost 5 ms together: these are on loopback.
    for span in trace.spans[1:]:
        span.duration = max(span.duration, 2_000)

    assert [(issue.kind, issue.count) for issue in issues(trace)] == [("blocking-call", 3)]
