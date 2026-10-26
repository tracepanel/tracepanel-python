"""A local stand-in for an LLM provider's API: each request gets the next answer a test queued, as JSON or as SSE.

The SDKs call it over HTTP as they would the real API (their httpx2 transport, so the HTTP call is a span too). It never
reaches a provider, and the clients get a key that is no key.
"""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tracepanel._compat import override

KEY = "test-key-not-a-real-one"


@dataclass
class Answer:
    status: int = 200
    body: object = None
    # Server-sent events, (event name or None, data): a stream instead of a body.
    events: list[tuple[str | None, object]] | None = None
    headers: dict[str, str] = field(default_factory=dict)


class Stub:
    def __init__(self) -> None:
        self.answers: list[Answer] = []
        # Each request's path, JSON body and traceparent, in order.
        self.received: list[tuple[str, Any, str | None]] = []
        self.url = ""

    def answer(self, body: object, status: int = 200, **headers: str) -> None:
        self.answers.append(Answer(status, body, headers=headers))

    def stream(self, events: list[tuple[str | None, object]]) -> None:
        self.answers.append(Answer(events=events))

    def reset(self) -> None:
        self.answers.clear()
        self.received.clear()


def _handler(stub: Stub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"null")
            stub.received.append((self.path, body, self.headers.get("traceparent")))
            answer = stub.answers.pop(0) if stub.answers else Answer(500, {"error": {"message": "nothing queued"}})
            if answer.events is not None:
                self._events(answer.events)
                return
            data = json.dumps(answer.body).encode()
            self.send_response(answer.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in answer.headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(data)

        def _events(self, events: list[tuple[str | None, object]]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for event, data in events:
                text = data if isinstance(data, str) else json.dumps(data)
                self.wfile.write((f"event: {event}\n" if event else "").encode() + f"data: {text}\n\n".encode())
                self.wfile.flush()
            self.close_connection = True

        @override
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 (http.server's signature)
            pass

    return Handler


def serve() -> Iterator[Stub]:
    """A stub on a free port of 127.0.0.1, for a module's tests."""
    stub = Stub()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    stub.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield stub
    httpd.shutdown()
    httpd.server_close()
