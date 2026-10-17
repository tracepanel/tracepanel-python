import re

import pytest
from django.test import Client

from tests.agentstore import AgentStore

pytestmark = pytest.mark.django_db


def test_the_ui_comes_from_the_agent_with_its_caching(client: Client, store: AgentStore) -> None:
    page = client.get("/__tracepanel__/")
    (script,) = re.findall(r'src="\./(tracepanel\.min\.js\?v=[0-9a-f]+)"', page.content.decode())
    routed = client.get("/__tracepanel__/traces/anything")
    widget = client.get("/__tracepanel__/widget.min.js")
    versioned = client.get(f"/__tracepanel__/{script}")
    unversioned = client.get("/__tracepanel__/tracepanel.min.js")

    # The page and widget.min.js are never cached (a new version must show); the page asks for its files with their
    # version: those are cached for good.
    assert [
        (r.status_code, r["Content-Type"].split(";")[0], r["Cache-Control"])
        for r in (page, routed, widget, versioned, unversioned)
    ] == [
        (200, "text/html", "no-cache"),
        # The UI routes in the browser: any page path is the page.
        (200, "text/html", "no-cache"),
        (200, "text/javascript", "no-cache"),
        (200, "text/javascript", "public, max-age=31536000, immutable"),
        (200, "text/javascript", "no-cache"),
    ]


@pytest.mark.parametrize("path", ["/__tracepanel__/../settings.py", "/__tracepanel__/assets/../../widget.min.js"])
def test_no_path_traversal(client: Client, store: AgentStore, path: str) -> None:
    assert client.get(path).status_code == 404


def test_the_agent_is_called_past_the_proxy_settings(
    client: Client, store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A proxy for every call, and urllib's shared opener built afresh: it would read them.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr("urllib.request._opener", None)

    # The agent is on this machine: the console reaches it without the proxy (on macOS, looking the system's proxy up
    # crashes a forked worker).
    assert client.get("/__tracepanel__/widget.min.js").status_code == 200
