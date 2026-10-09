"""The console's API from the project's agent: an app's console (Django's /__tracepanel__/) forwards to it.

Same origin and address for the UI; what only the framework can do (EXPLAIN, switching the user) stays in the app.
Without an agent, or when it doesn't answer, the console answers from the store itself.
"""

import http
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


def forward(request: Request, path: str) -> Response | None:
    """The agent's answer to `api/<path>`, or None without an agent that answers."""
    config = conf.get_config()
    if not agent.ensure_started(config) or (base := agent.console_url(config)) is None:
        return None
    url = f"{base}api/{path}" + (f"?{urlencode(dict(request.query))}" if request.query else "")
    response = _fetch(url, request.method)
    if (
        response is not None
        and response.status == http.HTTPStatus.NOT_FOUND
        and request.method == "GET"
        and path.startswith("traces/")
    ):
        # This process's own traces may still be in its queue, then in the agent's: send them, wait a little.
        transport.get_writer().flush(timeout=TRACE_WAIT_S)
        deadline = time.monotonic() + TRACE_WAIT_S
        while response is not None and response.status == http.HTTPStatus.NOT_FOUND and time.monotonic() < deadline:
            time.sleep(TRACE_POLL_S)
            response = _fetch(url, request.method)
    return response


def _fetch(url: str, method: str) -> Response | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=TIMEOUT_S) as answer:  # noqa: S310 (the agent's local address)
            return Response(status=answer.status, content_type=answer.headers.get_content_type(), body=answer.read())
    except urllib.error.HTTPError as error:
        return Response(status=error.code, content_type=error.headers.get_content_type(), body=error.read())
    except OSError:
        # A stale address file, or the agent stopping: the console answers itself.
        return None
