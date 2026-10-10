"""The console from the project's agent: an app's console (Django's /__tracepanel__/) forwards to it.

The UI, widget.min.js and the API (tracepanel-agent's schema/openapi.yaml) come from the agent at the app's own address:
the same origin for the widget's calls and the user's cookies. What only the framework can do stays in the app.
"""

import email.message
import http
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from tracepanel.console.messages import Request, Response
from tracepanel.core import conf
from tracepanel.storage import agent, transport

TIMEOUT_S = 10.0
# The widget asks for a trace as soon as its response arrives: the agent may not have written it yet.
TRACE_WAIT_S = 1.0
TRACE_POLL_S = 0.05
# The agent's headers the browser needs: versioned files and fonts are cached for good; the page, widget.min.js never.
PASSED = ("Cache-Control",)


def forward(request: Request, path: str) -> Response | None:
    """The agent's answer to `<path>` under the console, or None without an agent that answers."""
    config = conf.get_config()
    if not agent.ensure_started(config) or (base := agent.console_url(config)) is None:
        return None
    url = f"{base}{path}" + (f"?{urlencode(dict(request.query))}" if request.query else "")
    response = _fetch(url, request.method)
    if (
        response is not None
        and response.status == http.HTTPStatus.NOT_FOUND
        and request.method == "GET"
        and path.startswith("api/traces/")
    ):
        # This process's own traces may still be in its queue, then in the agent's: send them, wait a little.
        transport.get_writer().flush(timeout=TRACE_WAIT_S)
        deadline = time.monotonic() + TRACE_WAIT_S
        while response is not None and response.status == http.HTTPStatus.NOT_FOUND and time.monotonic() < deadline:
            time.sleep(TRACE_POLL_S)
            response = _fetch(url, request.method)
    return response


def trace(trace_id: str) -> dict[str, object] | None:
    """A stored trace as the API gives it (EXPLAIN reads its statement), or None."""
    response = forward(Request(method="GET", path=f"api/traces/{trace_id}"), f"api/traces/{trace_id}")
    if response is None or response.status != http.HTTPStatus.OK:
        return None
    document = json.loads(response.body)
    return document if isinstance(document, dict) else None


def _fetch(url: str, method: str) -> Response | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=TIMEOUT_S) as answer:  # noqa: S310 (the agent's local address)
            return _response(answer.status, answer.headers, answer.read())
    except urllib.error.HTTPError as error:
        return _response(error.code, error.headers, error.read())
    except OSError:
        # A stale address file, or the agent stopping.
        return None


def _response(status: int, headers: email.message.Message, body: bytes) -> Response:
    passed = {name: value for name in PASSED if (value := headers.get(name)) is not None}
    content_type = headers.get("Content-Type") or "application/octet-stream"
    return Response(status=status, content_type=content_type, body=body, headers=passed)
