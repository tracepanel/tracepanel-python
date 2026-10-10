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
