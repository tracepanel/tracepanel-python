"""`tracepanel console`: the console on its own, without the framework, reading the project's store.

For projects with no web server to host it (a Celery worker, management commands), or with the server stopped.
Python's own WSGI server, one thread per request; read-only (no EXPLAIN, no user switching: they need the framework).
"""

import http
import sys
from collections.abc import Callable, Iterable, Iterator
from socketserver import ThreadingMixIn
from typing import IO, Any
from urllib.parse import parse_qsl
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from tracepanel._compat import override
from tracepanel.console import app
from tracepanel.storage.store import get_store

StartResponse = Callable[..., Any]
CHUNK = 64 * 1024


def wsgi(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    request = app.Request(
        method=str(environ.get("REQUEST_METHOD", "GET")),
        path=str(environ.get("PATH_INFO", "/")).lstrip("/"),
        query=dict(parse_qsl(str(environ.get("QUERY_STRING", "")))),
    )
    response = app.dispatch(request)
    status = f"{response.status} {http.HTTPStatus(response.status).phrase}"
    start_response(status, [("Content-Type", response.content_type), *response.headers.items()])
    if response.file is not None:
        wrapper = environ.get("wsgi.file_wrapper")
        # The server's wrapper closes the file when it is done; without one, the generator does.
        return wrapper(response.file, CHUNK) if wrapper else _chunks(response.file)
    return [response.body]


def _chunks(file: IO[bytes]) -> Iterator[bytes]:
    with file:
        while chunk := file.read(CHUNK):
            yield chunk


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    @override
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (the base class's signature)
        del format, args


def serve(host: str, port: int) -> None:
    server = make_server(host, port, wsgi, server_class=_Server, handler_class=_QuietHandler)
    sys.stdout.write(f"tracepanel console: http://{host}:{port}/  (store: {get_store().path})\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
