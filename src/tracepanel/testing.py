"""Tests that hold the app to what tracepanel finds: the queries a block makes, the issues the agent finds in a test.

    with tracepanel.max_queries(3):
        client.get("/posts/")

The pytest plugin (tracepanel.pytest_plugin) runs each test as a trace of its own, kind `test`, named after the test.
A request the test makes of the app through its test client (Django's Client and AsyncClient, Starlette's and
FastAPI's TestClient, httpx's ASGI and WSGI transports, Flask's and Werkzeug's test client) is an `http.client` span
of that trace, and the app's trace of the request is a trace of its own that the request's `traceparent` links to it
(`called_by`), as a call between two apps is (tracepanel.core.w3c). A test's queries are its own and those of the
requests it made, and the agent's findings in all of those traces are the test's.
"""

import contextlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any, TypeVar
from urllib.parse import urlencode

from tracepanel.core import errors, w3c
from tracepanel.core.context import TEST, TraceBlock, current_trace, now_us, start_trace
from tracepanel.core.ids import new_id
from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs, Span, Trace
from tracepanel.core.patching import patch
from tracepanel.integrations.http_clients import _finish  # the same span as an HTTP client's call
from tracepanel.storage import transport

T = TypeVar("T")

# What the agent finds (its schema/trace.schema.json): the kinds a test can fail on.
KINDS = (
    "n+1",
    "duplicates",
    "slow-sql",
    "slow-http",
    "slow-call",
    "error",
    "long-transaction",
    "in-transaction",
    "htmx",
    "queue-wait",
    "blocking-call",
    "large-result",
)
# How long to wait for the agent to store a test's traces, and how often to ask.
STORED_WAIT_S = 5.0
POLL_S = 0.01
# A failure lists this many queries, and says how many more there were.
MAX_LISTED = 20
# A statement longer than this is cut in a failure's message.
MAX_STATEMENT = 300


# ---------- the traces a block's requests made ----------

_watchers: list[list[Trace]] = []


def _completed(trace: Trace) -> None:
    # A copy: a block may end in another thread meanwhile.
    for served in _watchers[:]:
        served.append(trace)


@contextlib.contextmanager
def watching() -> Iterator[list[Trace]]:
    """The traces completed while inside, in this process: the app's traces of the requests the block made."""
    served: list[Trace] = []
    transport.on_complete(_completed)
    _watchers.append(served)
    try:
        yield served
    finally:
        _watchers.remove(served)


def queries(spans: list[Span]) -> list[Span]:
    return [span for span in spans if span.kind == "sql"]


def _statement(span: Span) -> str:
    text = " ".join(str(span.attrs.get("db.statement") or span.name).split())
    return text if len(text) <= MAX_STATEMENT else f"{text[:MAX_STATEMENT]}…"


def listed(spans: list[Span]) -> str:
    """Queries as a failure lists them: the statement, and where the project's code made it."""
    lines = []
    for span in spans[:MAX_LISTED]:
        where = span.attrs.get("code.origin")
        lines.append(f"  {_statement(span)}" + (f"\n    at {where}" if where else ""))
    if len(spans) > MAX_LISTED:
        lines.append(f"  … and {len(spans) - MAX_LISTED} more")
    return "\n".join(lines)


def too_many(made: list[Span], limit: int) -> str:
    return f"{len(made)} queries, at most {limit} expected:\n{listed(made)}"


class _MaxQueries:
    """max_queries()'s block: its exit fails, and pytest leaves this frame out of the failure's traceback."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._trace: Trace | None = None
        self._since = 0
        self._watch = contextlib.ExitStack()
        self._served: list[Trace] = []

    def __enter__(self) -> None:
        trace = current_trace()
        if trace is None:
            msg = "tracepanel.max_queries() counts the queries of a trace, and none is running: run pytest --tracepanel"
            raise RuntimeError(msg)
        self._trace, self._since = trace, len(trace.spans)
        self._served = self._watch.enter_context(watching())

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        __tracebackhide__ = True
        self._watch.close()
        if exc_type is not None or self._trace is None:
            return
        own = queries(self._trace.spans[self._since :])
        made = [*own, *(span for other in self._served for span in queries(other.spans))]
        if len(made) > self.limit:
            raise AssertionError(too_many(made, self.limit))


def max_queries(limit: int) -> contextlib.AbstractContextManager[None]:
    """Fail (AssertionError) when the block makes more than `limit` SQL queries, those of its requests included.

        with tracepanel.max_queries(3):
            client.get("/posts/")

    It counts in the current trace: a test's, with tracepanel's pytest plugin on.
    """
    return _MaxQueries(limit)


# ---------- what the agent found ----------


@dataclass(frozen=True, slots=True)
class Finding:
    """An issue the agent found in a test's trace, or in the trace of a request the test made."""

    kind: str
    title: str
    # Where in the project's code: `blog/views.py:21 in post_list`.
    culprit: str | None
    hint: str | None
    count: int
    # The statement of its first query, for an issue about SQL.
    query: str | None
    trace_id: str
    trace_name: str
    # The trace in the agent's console.
    url: str

    def describe(self, test: str | None = None) -> str:
        """The finding as a failure lists it; the trace it is in unless it is the test's own (`test`, its id)."""
        lines = [self.title]
        if self.query:
            lines.append(f"  {self.query}")
        if self.culprit:
            lines.append(f"  at {self.culprit}")
        if self.trace_id != test:
            lines.append(f"  in {self.trace_name} → {self.url}")
        if self.hint:
            lines.append(f"  {self.hint}")
        return "\n".join(lines)


def trace_url(console: str, trace_id: str) -> str:
    return f"{console.rstrip('/')}/#/t/{trace_id}"


class Console:
    """The agent's console API, where a test's traces are read back with what the agent found in them."""

    def __init__(self, url: str) -> None:
        self.url = url

    def _get(self, path: str, query: dict[str, str] | None = None) -> Any:  # noqa: ANN401 (JSON)
        address = f"{self.url.rstrip('/')}/api/{path}" + (f"?{urlencode(query)}" if query else "")
        try:
            with urllib.request.urlopen(address, timeout=STORED_WAIT_S) as answer:  # noqa: S310 (the agent, on this machine)
                return json.loads(answer.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:  # noqa: PLR2004
                return None
            raise

    def stored(self, ids: list[str], timeout: float = STORED_WAIT_S) -> dict[str, dict[str, Any]]:
        """The traces' summaries once the agent has stored them all (it writes in a thread of its own), by id."""
        deadline = time.monotonic() + timeout
        while True:
            answer = self._get("traces", {"ids": ",".join(ids), "limit": str(len(ids))})
            found = {row["id"]: row for row in (answer or {}).get("traces", [])}
            if len(found) >= len(ids) or time.monotonic() >= deadline:
                return found
            time.sleep(POLL_S)

    def findings(self, traces: list[Trace]) -> list[Finding]:
        """What the agent found in these traces, the oldest trace's first."""
        if not traces:
            return []
        spans = {span.id: span for trace in traces for span in trace.spans}
        stored = self.stored([trace.id for trace in traces])
        found: list[Finding] = []
        for trace in sorted(traces, key=lambda trace: trace.start):
            summary = stored.get(trace.id)
            if not summary or not summary.get("issues"):
                continue
            document = self._get(f"traces/{trace.id}") or {}
            for issue in document.get("issues") or []:
                first = next((spans[span] for span in issue.get("span_ids") or [] if span in spans), None)
                found.append(
                    Finding(
                        kind=issue["kind"],
                        title=issue["title"],
                        culprit=issue.get("culprit"),
                        hint=issue.get("hint"),
                        count=issue.get("count", 1),
                        query=_statement(first) if first is not None and first.kind == "sql" else None,
                        trace_id=trace.id,
                        trace_name=trace.name,
                        url=trace_url(self.url, trace.id),
                    )
                )
        return found


# ---------- a test's trace ----------

# The tests running now, between their start() and stop(): one, but for a test that runs another (pytester).
_running: list["TracedTest"] = []


def running() -> bool:
    """Whether a test is running: what else completes now (a fixture's request, a test database's migrate) is not."""
    return bool(_running)


class TracedTest:
    """A test run as a trace: what the pytest plugin records, and what the `tracepanel_trace` fixture gives a test.

    assert tracepanel_trace.queries <= 5
    assert not tracepanel_trace.issues
    """

    __test__ = False

    def __init__(self, name: str, attrs: Attrs, console: Console | None) -> None:
        self.name, self.attrs, self.console = name, attrs, console
        self.trace: Trace | None = None
        # The app's traces of the requests the test made.
        self.served: list[Trace] = []
        self._block: TraceBlock | None = None
        self._watch: contextlib.ExitStack | None = None

    def start(self) -> None:
        """The test begins: its trace is current, in the context the test's code runs in."""
        self._block = start_trace(TEST, self.name, self.attrs, root_kind="function")
        self.trace = self._block.__enter__()
        self._watch = contextlib.ExitStack()
        self.served = self._watch.enter_context(watching())
        _running.append(self)

    def stop(self, failure: BaseException | None = None) -> None:
        """The test ended, failed with `failure` (an error of its trace); in the context it began in."""
        block, self._block = self._block, None
        if block is None:
            return
        with contextlib.suppress(ValueError):
            _running.remove(self)
        if self._watch is not None:
            self._watch.close()
        if failure is not None:
            errors.capture(failure, handled=False)
            block.__exit__(type(failure), failure, failure.__traceback__)
        else:
            block.__exit__(None, None, None)

    def send(self) -> None:
        if self.trace is not None:
            transport.complete(self.trace)

    @property
    def traces(self) -> list[Trace]:
        """The test's trace and those of its requests, as far as they are recorded."""
        own = [self.trace] if self.trace is not None else []
        return [*own, *[trace for trace in self.served if trace is not self.trace]]

    @property
    def sql(self) -> list[Span]:
        """The queries the test made so far, its requests' included."""
        return [span for trace in self.traces for span in queries(trace.spans)]

    @property
    def queries(self) -> int:
        return len(self.sql)

    @property
    def issues(self) -> list[Finding]:
        """What the agent found so far: in the requests the test made; its own trace's once the test has ended.

        Empty without an agent.
        """
        if self.console is None:
            return []
        done = self.traces if self._block is None and self.trace is not None else list(self.served)
        return self.console.findings(done)

    @property
    def url(self) -> str | None:
        """The test's trace in the agent's console."""
        return trace_url(self.console.url, self.trace.id) if self.console and self.trace else None


# ---------- the requests a test makes of the app ----------


def _within_test() -> Trace | None:
    trace = current_trace()
    return trace if trace is not None and trace.kind == TEST else None


def _served(method: str, url: str, send: Callable[[str | None], T], status: Callable[[T], int | None]) -> T:
    """A request the test makes of the app in its process: a call of the test's trace, whose traceparent `send` adds."""
    trace = _within_test()
    if trace is None:
        return send(None)
    span = new_id()
    started = now_us(), time.perf_counter_ns()
    try:
        response = send(w3c.outgoing(trace, span))
    except Exception as exc:
        _finish(method, url, started, span, status_code=None, failed=type(exc).__name__)
        raise
    _finish(method, url, started, span, status_code=status(response), failed=None)
    return response


async def _served_async(
    method: str, url: str, send: Callable[[str | None], Awaitable[T]], status: Callable[[T], int | None]
) -> T:
    trace = _within_test()
    if trace is None:
        return await send(None)
    span = new_id()
    started = now_us(), time.perf_counter_ns()
    try:
        response = await send(w3c.outgoing(trace, span))
    except Exception as exc:
        _finish(method, url, started, span, status_code=None, failed=type(exc).__name__)
        raise
    _finish(method, url, started, span, status_code=status(response), failed=None)
    return response


def _django(module: ModuleType) -> None:
    """Django's Client (a WSGI environ's keys) and AsyncClient (an ASGI scope's)."""
    client, async_client = module.Client, module.AsyncClient
    request, request_async = client.request, async_client.request
    header = "HTTP_" + w3c.HEADER.upper()

    def traced(self: Any, **environ: Any) -> Any:  # noqa: ANN401 (Django's test client)
        query = environ.get("QUERY_STRING")
        url = f"http://testserver{environ.get('PATH_INFO', '/')}" + (f"?{query}" if query else "")

        def send(traceparent: str | None) -> Any:  # noqa: ANN401
            if traceparent is None or header in environ:
                return request(self, **environ)
            return request(self, **{**environ, header: traceparent})

        return _served(str(environ.get("REQUEST_METHOD", "GET")), url, send, lambda r: r.status_code)

    async def traced_async(self: Any, **scope: Any) -> Any:  # noqa: ANN401
        query = scope.get("query_string")
        query = query.decode("latin-1") if isinstance(query, bytes) else query
        url = f"http://testserver{scope.get('path', '/')}" + (f"?{query}" if query else "")
        headers = list(scope.get("headers") or [])

        def send(traceparent: str | None) -> Awaitable[Any]:
            if traceparent is None or any(name.lower() == w3c.HEADER.encode() for name, _ in headers):
                return request_async(self, **scope)
            return request_async(self, **{**scope, "headers": [*headers, (w3c.HEADER.encode(), traceparent.encode())]})

        return await _served_async(str(scope.get("method", "GET")), url, send, lambda r: r.status_code)

    patch(client, "request", traced)
    patch(async_client, "request", traced_async)


def _werkzeug(module: ModuleType) -> None:
    """Werkzeug's test client, which Flask's is: every request goes through run_wsgi_app()."""
    client = module.Client
    run = client.run_wsgi_app
    header = "HTTP_" + w3c.HEADER.upper()

    def traced(self: Any, environ: dict[str, Any], buffered: bool = False) -> Any:  # noqa: ANN401, FBT001, FBT002 (Werkzeug's)
        host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "localhost")
        query = environ.get("QUERY_STRING")
        path = f"{environ.get('SCRIPT_NAME', '')}{environ.get('PATH_INFO', '/')}" + (f"?{query}" if query else "")
        url = f"{environ.get('wsgi.url_scheme', 'http')}://{host}{path}"

        def send(traceparent: str | None) -> Any:  # noqa: ANN401
            if traceparent is None or header in environ:
                return run(self, environ, buffered)
            return run(self, {**environ, header: traceparent}, buffered)

        return _served(str(environ.get("REQUEST_METHOD", "GET")), url, send, lambda r: int(str(r[1]).split()[0]))

    patch(client, "run_wsgi_app", traced)


def _carry(request: Any, traceparent: str | None) -> Any:  # noqa: ANN401 (an httpx Request)
    if traceparent is not None and w3c.HEADER not in request.headers:
        request.headers[w3c.HEADER] = traceparent
    return request


def _transport(transport_class: type) -> None:
    """A transport that hands the request to the app in this process: Starlette's TestClient's, httpx's WSGI one."""
    handle = transport_class.handle_request

    def traced(self: Any, request: Any) -> Any:  # noqa: ANN401 (an httpx Request and Response)
        if request.headers.get("upgrade", "").lower() == "websocket":
            # A WebSocket session the test client opens: not a request with a response.
            return handle(self, request)
        return _served(
            request.method,
            str(request.url),
            lambda traceparent: handle(self, _carry(request, traceparent)),
            lambda r: r.status_code,
        )

    patch(transport_class, "handle_request", traced)


def _async_transport(transport_class: type) -> None:
    handle = transport_class.handle_async_request

    async def traced(self: Any, request: Any) -> Any:  # noqa: ANN401
        return await _served_async(
            request.method,
            str(request.url),
            lambda traceparent: handle(self, _carry(request, traceparent)),
            lambda r: r.status_code,
        )

    patch(transport_class, "handle_async_request", traced)


def _starlette(module: ModuleType) -> None:
    _transport(module._TestClientTransport)  # noqa: SLF001 (what TestClient sends every request through)


def _httpx(module: ModuleType) -> None:
    _transport(module.WSGITransport)
    _async_transport(module.ASGITransport)


def install() -> None:
    """Make each test client's requests calls of the test's trace, once the tests import it."""
    when_imported("django.test.client", _django)
    when_imported("werkzeug.test", _werkzeug)
    when_imported("starlette.testclient", _starlette)
    for name in ("httpx", "httpx2"):
        when_imported(name, _httpx)
