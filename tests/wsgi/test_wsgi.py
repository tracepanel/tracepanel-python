from collections.abc import Callable, Generator, Iterable, Iterator
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest
from dirty_equals import IsStr

import tracepanel.wsgi
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


def call(wsgi: tracepanel.wsgi.WSGIApp, path: str) -> tuple[str, dict[str, str], bytes]:
    environ: dict[str, Any] = {"PATH_INFO": path}
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
        "Server-Timing": IsStr(regex=r'db;dur=0\.0;desc="0 queries", app;dur=[\d.]+, total;dur=[\d.]+'),
    }


def test_widget_in_html_pages(wrapped: tracepanel.wsgi.WSGIApp, traces: list[Trace]) -> None:
    _, headers, body = call(wrapped, "/page")
    page = body.decode()

    assert page.index("<head>") < page.index('<script src="/__tracepanel__/widget.js"') < page.index("<title>")
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


def test_console_is_served(wrapped: tracepanel.wsgi.WSGIApp) -> None:
    status, headers, _ = call(wrapped, "/__tracepanel__/api/counts")

    assert (status, headers["Content-Type"]) == ("200 OK", IsStr(regex=r"application/json.*"))


def test_off_unless_enabled(traces: list[Trace]) -> None:
    previous = conf._framework  # noqa: SLF001

    assert tracepanel.wsgi.install(app) is app

    conf.use_source(previous)
