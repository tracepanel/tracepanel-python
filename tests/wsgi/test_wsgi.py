from collections.abc import Callable, Generator, Iterable, Iterator
from io import BytesIO
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest
from dirty_equals import IsStr

import tracepanel.wsgi
from tests.agentstore import AgentStore, issues
from tracepanel.core import conf
from tracepanel.core.model import Trace

StartResponse = Callable[..., Any]
PAGE = b"<html><head><title>t</title></head><body>hi</body></html>"


def app(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    path = environ["PATH_INFO"]
    if path == "/boom":
        message = "nope"
        raise ValueError(message)
    if path == "/page":
        start_response("200 OK", [("Content-Type", "text/html"), ("Content-Length", str(len(PAGE)))])
        return [PAGE]
    if path == "/stream":
        return stream(start_response)
    start_response("201 Created", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return [b"ok"]


def stream(start_response: StartResponse) -> Iterator[bytes]:
    # A generator app: start_response comes with the first chunk, and there is no Content-Length.
    start_response("200 OK", [("Content-Type", "text/plain")])
    yield b"a"
    yield b"b"


@pytest.fixture
def wrapped(traces: list[Trace]) -> Generator[tracepanel.wsgi.WSGIApp, None, None]:
    previous = conf._framework  # noqa: SLF001
    yield tracepanel.wsgi.install(app, enabled=True)
    # The Django tests go on with their own settings.
    conf.use_source(previous)


def call(
    wsgi: tracepanel.wsgi.WSGIApp, path: str, environ: dict[str, Any] | None = None
) -> tuple[str, dict[str, str], bytes]:
    environ = {"PATH_INFO": path, **(environ or {})}
    setup_testing_defaults(environ)
    started: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: object = None) -> None:
        del exc_info
        started.update(status=status, headers=dict(headers))

    result = wsgi(environ, start_response)
    body = b"".join(result)
    close = getattr(result, "close", None)
    if close is not None:
        close()
    return started["status"], started["headers"], body


def test_any_wsgi_app(wrapped: tracepanel.wsgi.WSGIApp, traces: list[Trace]) -> None:
    status, headers, body = call(wrapped, "/orders/5")
    (trace,) = traces

    assert (status, body) == ("201 Created", b"ok")
    assert (trace.name, trace.attrs["http.status_code"], trace.status) == ("GET /orders/5", 201, "ok")
    assert headers == {
        "Content-Type": "text/plain",
        "Content-Length": "2",
        "Tracepanel-Id": trace.id,
        "Tracepanel-Url": f"http://127.0.0.1/__tracepanel__/#/t/{trace.id}",
        "Server-Timing": IsStr(
            regex=r'db;dur=0\.0;desc="0 queries", app;dur=[\d.]+, total;dur=[\d.]+, tracepanel-issues;desc="pending"'
        ),
    }


def test_widget_in_html_pages(wrapped: tracepanel.wsgi.WSGIApp, traces: list[Trace], store: AgentStore) -> None:
    _, headers, body = call(wrapped, "/page")
    page = body.decode()

    assert page.index("<head>") < page.index('<script src="/__tracepanel__/widget.min.js"') < page.index("<title>")
    assert f'data-tracepanel-id="{traces[0].id}"' in page
    assert headers["Content-Length"] == str(len(body))


def test_streamed_responses_go_out_as_made(wrapped: tracepanel.wsgi.WSGIApp, traces: list[Trace]) -> None:
    status, headers, body = call(wrapped, "/stream")

    assert (status, body) == ("200 OK", b"ab")
    assert headers == {"Content-Type": "text/plain", "Tracepanel-Id": traces[0].id}


def test_errors_through_to_the_server(wrapped: tracepanel.wsgi.WSGIApp, traces: list[Trace]) -> None:
    environ: dict[str, Any] = {"PATH_INFO": "/boom"}
    setup_testing_defaults(environ)

    def start_response(*args: object) -> None:
        del args

    with pytest.raises(ValueError, match="nope"):
        wrapped(environ, start_response)

    (trace,) = traces
    assert (trace.status, trace.attrs["http.status_code"]) == ("error", 500)
    assert [(error.type, error.handled) for error in trace.errors] == [("builtins.ValueError", False)]
    # Where a debugger's page finds the trace.
    assert (environ[tracepanel.wsgi.TRACE_KEY], environ[tracepanel.wsgi.URL_KEY]) == (
        trace.id,
        "http://127.0.0.1/__tracepanel__/",
    )


def test_console_is_served(wrapped: tracepanel.wsgi.WSGIApp, store: AgentStore) -> None:
    status, headers, _ = call(wrapped, "/__tracepanel__/api/counts")

    assert (status, headers["Content-Type"]) == ("200 OK", IsStr(regex=r"application/json.*"))


def test_off_unless_enabled(traces: list[Trace]) -> None:
    previous = conf._framework  # noqa: SLF001

    assert tracepanel.wsgi.install(app) is app

    conf.use_source(previous)


def test_a_request_from_another_app_continues_its_trace(wrapped: tracepanel.wsgi.WSGIApp, traces: list[Trace]) -> None:
    call(wrapped, "/page", {"HTTP_TRACEPARENT": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
    (trace,) = traces

    assert (trace.attrs["w3c.trace_id"], trace.attrs["w3c.parent_span_id"]) == (
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "00f067aa0ba902b7",
    )


def test_htmx_gets_a_whole_page(wrapped: tracepanel.wsgi.WSGIApp, traces: list[Trace]) -> None:
    call(wrapped, "/page", {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "rows"})
    (trace,) = traces

    assert {key: value for key, value in trace.attrs.items() if key.startswith("htmx.")} == {
        "htmx.request": True,
        "htmx.target": "rows",
        "htmx.full_page": True,
    }
    assert [issue.title for issue in issues(trace)] == ["Whole page sent to htmx · GET /page"]


def test_writes_through_the_console_come_from_this_machine_only(
    wrapped: tracepanel.wsgi.WSGIApp, store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ingest(address: str) -> int:
        environ = {
            "REQUEST_METHOD": "POST",
            "REMOTE_ADDR": address,
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": "2",
            "wsgi.input": BytesIO(b"[]"),
        }
        # The code: the reason's wording changes between Pythons (413 is "Content Too Large" from 3.13).
        return int(call(wrapped, "/__tracepanel__/api/ingest", environ)[0].split()[0])

    assert (ingest("10.0.0.5"), ingest("127.0.0.1")) == (403, 202)

    monkeypatch.setattr("tracepanel.wsgi.MAX_BODY", 1)

    assert ingest("127.0.0.1") == 413


def reads_its_body(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    length = int(environ.get("CONTENT_LENGTH") or 0)
    read = environ["wsgi.input"].read(length) if environ["PATH_INFO"] == "/read" else b""
    start_response(
        "200 OK", [("Content-Type", "text/plain"), ("Set-Cookie", "sessionid=s"), ("X-Read", str(len(read)))]
    )
    return [b"ok"]


@pytest.fixture
def reading(traces: list[Trace]) -> Generator[tracepanel.wsgi.WSGIApp, None, None]:
    previous = conf._framework  # noqa: SLF001
    yield tracepanel.wsgi.install(reads_its_body, enabled=True)
    conf.use_source(previous)


def posted(body: bytes) -> dict[str, Any]:
    return {
        "REQUEST_METHOD": "POST",
        "QUERY_STRING": "page=2&token=abc",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_X_REQUEST_ID": "r-1",
        "HTTP_COOKIE": "theme=dark; sessionid=s-1",
        "wsgi.input": BytesIO(body),
    }


def test_a_request_records_what_it_carried_with_secrets_masked(
    reading: tracepanel.wsgi.WSGIApp, traces: list[Trace]
) -> None:
    body = b'{"email": "ann@example.com", "password": "p"}'

    call(reading, "/read", posted(body))

    (trace,) = traces
    assert {
        key: value for key, value in trace.attrs.items() if key.startswith(("http.request.", "http.response.", "url."))
    } == {
        "http.request.header.content_type": "application/json",
        "http.request.header.content_length": str(len(body)),
        "http.request.header.x_request_id": "r-1",
        "http.request.header.host": "127.0.0.1",
        "url.query": "page=2&token=********",
        "http.request.cookie.theme": "dark",
        "http.request.cookie.sessionid": "********",
        "http.response.header.content_type": "text/plain",
        "http.response.header.set_cookie": "********",
        "http.response.header.x_read": str(len(body)),
        "http.request.body": '{"email":"ann@example.com","password":"********"}',
    }


def test_a_body_the_app_never_read_is_not_read_for_it(reading: tracepanel.wsgi.WSGIApp, traces: list[Trace]) -> None:
    call(reading, "/ignore", posted(b'{"password": "p"}'))

    (trace,) = traces
    assert "http.request.body" not in trace.attrs
    assert trace.attrs["http.response.header.x_read"] == "0"
