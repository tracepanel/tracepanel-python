"""The servers' access logs leave out tracepanel's own requests, each server's record as it logs it."""

import contextlib
import io
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tracepanel._compat import override
from tracepanel.core import accesslog, conf

OURS = "/__tracepanel__/api/traces?ids=a1&runs=1"
APP = "/items/1?page=2"


@pytest.fixture
def quiet() -> Generator[None, None, None]:
    accesslog.install()
    yield
    accesslog.uninstall()


class Collected(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def collected(name: str) -> Iterator[list[str]]:
    """The messages the logger hands its handlers (some servers' loggers don't propagate: caplog wouldn't see them)."""
    logger = logging.getLogger(name)
    handler, level = Collected(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def logged(name: str, *calls: tuple[object, ...]) -> list[str]:
    """What the logger lets through of the records logged with these arguments."""
    with collected(name) as messages:
        for msg, *args in calls:
            logging.getLogger(name).info(msg, *args)
    return messages


def uvicorn_call(path: str) -> tuple[object, ...]:
    # uvicorn.protocols.http.*_impl: the client, the method, the path with its query, the version, the status.
    return ('%s - "%s %s HTTP/%s" %d', "127.0.0.1:57161", "GET", path, "1.1", 200)


def atoms_call(path: str) -> tuple[object, ...]:
    # gunicorn's and hypercorn's atoms: "U" the path, "r" the request line.
    atoms = {"h": "127.0.0.1", "r": f"GET {path} HTTP/1.1", "s": "200", "U": path.partition("?")[0]}
    return ('%(h)s "%(r)s" %(s)s', atoms)


def granian_call(path: str) -> tuple[object, ...]:
    # granian.log's default access format, the path without its query.
    path, _, query = path.partition("?")
    atoms = {"addr": "127.0.0.1", "method": "GET", "path": path, "query_string": query, "protocol": "HTTP/1.1"}
    return ('%(addr)s - "%(method)s %(path)s %(protocol)s"', atoms)


def test_uvicorn(quiet: None) -> None:
    messages = logged("uvicorn.access", uvicorn_call(OURS), uvicorn_call(APP))

    assert messages == ['127.0.0.1:57161 - "GET /items/1?page=2 HTTP/1.1" 200']


@pytest.mark.parametrize("name", ["gunicorn.access", "hypercorn.access"])
def test_gunicorn_and_hypercorn(quiet: None, name: str) -> None:
    messages = logged(name, atoms_call(OURS), atoms_call(APP))

    assert messages == ['127.0.0.1 "GET /items/1?page=2 HTTP/1.1" 200']


def test_granian(quiet: None) -> None:
    messages = logged("granian.access", granian_call(OURS), granian_call(APP))

    assert messages == ['127.0.0.1 - "GET /items/1 HTTP/1.1"']


def test_django_runserver(quiet: None) -> None:
    from django.core.servers.basehttp import WSGIRequestHandler  # noqa: PLC0415

    handler = WSGIRequestHandler.__new__(WSGIRequestHandler)
    handler.request = None  # type: ignore[assignment]
    with collected("django.server") as messages:
        handler.log_message('"%s" %s %s', f"GET {OURS} HTTP/1.1", "200", "15")
        handler.log_message('"%s" %s %s', f"GET {APP} HTTP/1.1", "200", "15")

    assert messages == ['"GET /items/1?page=2 HTTP/1.1" 200 15']


@pytest.mark.parametrize("code", [200, 304])
def test_flask_development_server(monkeypatch: pytest.MonkeyPatch, quiet: None, code: int) -> None:
    serving = pytest.importorskip("werkzeug.serving")
    # Colors, as in a terminal: a 304's request line is cyan.
    monkeypatch.setattr(serving, "_log_add_style", True)

    with collected("werkzeug") as messages:
        for path in (OURS, APP):
            handler = serving.WSGIRequestHandler.__new__(serving.WSGIRequestHandler)
            handler.client_address = ("127.0.0.1", 57161)
            handler.command, handler.path, handler.request_version = "GET", path, "HTTP/1.1"
            # 304 in ANSI colors when the terminal has them.
            handler.log_request(code, 15)

    assert len(messages) == 1
    assert "GET /items/1?page=2 HTTP/1.1" in messages[0]


def test_channels_runserver(quiet: None) -> None:
    pytest.importorskip("daphne")
    from daphne.management.commands.runserver import Command  # noqa: PLC0415

    command = Command(no_color=True)
    with collected("django.channels.server") as messages:
        for path in ("/__tracepanel__/api/traces", "/items/1"):
            details = {"method": "GET", "path": path, "status": 200, "time_taken": 0.01, "client": "127.0.0.1:57161"}
            command.log_action("http", "complete", details)

    assert messages == ["HTTP GET /items/1 200 [0.01, 127.0.0.1:57161]"]


def test_daphne_writes_its_access_log_without_ours(quiet: None) -> None:
    pytest.importorskip("daphne")
    from daphne.access import AccessLogGenerator  # noqa: PLC0415

    stream = io.StringIO()
    generator = AccessLogGenerator(stream)

    for path in ("/__tracepanel__/api/traces", "/items/1"):
        generator("http", "complete", {"client": "127.0.0.1", "method": "GET", "path": path, "status": 200, "size": 2})

    assert [line.split('"')[1] for line in stream.getvalue().splitlines()] == ["GET /items/1"]


def test_only_the_prefix_itself_is_ours(quiet: None) -> None:
    paths = ("/__tracepanel__", "/__tracepanel__/", "/__tracepanel__?x=1", "/__tracepanel__x/", "/app/__tracepanel__/")

    messages = logged("uvicorn.access", *(uvicorn_call(path) for path in paths))

    assert messages == [
        '127.0.0.1:57161 - "GET /__tracepanel__x/ HTTP/1.1" 200',
        '127.0.0.1:57161 - "GET /app/__tracepanel__/ HTTP/1.1" 200',
    ]


def test_another_prefix(monkeypatch: pytest.MonkeyPatch, quiet: None) -> None:
    monkeypatch.setenv("TRACEPANEL_PREFIX", "/_debug/")
    conf.reset()

    messages = logged("uvicorn.access", uvicorn_call("/_debug/api/traces"), uvicorn_call(OURS))

    assert messages == ['127.0.0.1:57161 - "GET /__tracepanel__/api/traces?ids=a1&runs=1 HTTP/1.1" 200']


def test_the_setting_off_keeps_them(monkeypatch: pytest.MonkeyPatch) -> None:
    # A framework set up by an earlier test installed it.
    accesslog.uninstall()
    monkeypatch.setenv("TRACEPANEL_QUIET_ACCESS_LOG", "false")
    conf.reset()
    accesslog.install()

    messages = logged("uvicorn.access", uvicorn_call(OURS))

    assert conf.get_config().quiet_access_log is False
    assert messages == ['127.0.0.1:57161 - "GET /__tracepanel__/api/traces?ids=a1&runs=1 HTTP/1.1" 200']


def test_the_app_logging_on_a_server_logger_is_kept(quiet: None) -> None:
    messages = logged("uvicorn.access", ("no arguments /__tracepanel__/",), ("%d requests", 3))

    assert messages == ["no arguments /__tracepanel__/", "3 requests"]


# A real server in a subprocess: it sets up its logging (dictConfig, its handlers) around the app's import.
SERVED = """
import tracepanel.asgi
import tracepanel.wsgi

async def asgi(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

def wsgi(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]

asgi = tracepanel.asgi.install(asgi, enabled=True)
wsgi = tracepanel.wsgi.install(wsgi, enabled=True)
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def serve(tmp_path: Path, *command: str, port: int) -> str:
    """The server's output after a request of tracepanel's and two of the app's; the server is stopped."""
    (tmp_path / "served.py").write_text(SERVED)
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TRACEPANEL_")}
    server = subprocess.Popen(  # noqa: S603 (this interpreter, a server module)
        [sys.executable, "-m", *command],
        cwd=tmp_path,
        env={**clean, "TRACEPANEL_AGENT": "off", "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        for path in (APP, OURS, "/after"):
            with contextlib.suppress(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).close()
        # The access log is written as each response is sent; give the last one a moment.
        time.sleep(0.5)
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=20)
    return output


@pytest.mark.parametrize(
    "server",
    [
        ("uvicorn", "served:asgi", "--host", "127.0.0.1", "--port", "{port}"),
        (
            "gunicorn",
            "served:wsgi",
            "--bind",
            "127.0.0.1:{port}",
            "--access-logfile",
            "-",
            "--no-control-socket",
        ),
    ],
    ids=["uvicorn", "gunicorn"],
)
def test_a_running_server(tmp_path: Path, server: tuple[str, ...]) -> None:
    pytest.importorskip(server[0])
    port = free_port()

    output = serve(tmp_path, *(part.format(port=port) for part in server), port=port)
    requests = [line for line in output.splitlines() if '"GET ' in line]

    assert len(requests) == 2, output
    assert "GET /items/1?page=2 HTTP/1.1" in requests[0]
    assert "GET /after HTTP/1.1" in requests[1]
