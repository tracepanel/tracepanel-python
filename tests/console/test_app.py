import re

import pytest
from django.test import Client

from tests.agentstore import AgentStore

pytestmark = pytest.mark.django_db


def test_the_ui_comes_from_the_agent_with_its_caching(client: Client, store: AgentStore) -> None:
    page = client.get("/__tracepanel__/")
    (asset,) = re.findall(r'src="\.?/?(assets/[^"]+\.js)"', page.content.decode())[:1]
    routed = client.get("/__tracepanel__/traces/anything")
    widget = client.get("/__tracepanel__/widget.js")
    built = client.get(f"/__tracepanel__/{asset}")

    # The page and widget.js are never cached (a new version must show); built assets carry a hash: cached for good.
    assert [
        (r.status_code, r["Content-Type"].split(";")[0], r["Cache-Control"]) for r in (page, routed, widget, built)
    ] == [
        (200, "text/html", "no-cache"),
        # The UI routes in the browser: any page path is the page.
        (200, "text/html", "no-cache"),
        (200, "text/javascript", "no-cache"),
        (200, "text/javascript", "public, max-age=31536000, immutable"),
    ]


@pytest.mark.parametrize("path", ["/__tracepanel__/../settings.py", "/__tracepanel__/assets/../../widget.js"])
def test_no_path_traversal(client: Client, store: AgentStore, path: str) -> None:
    assert client.get(path).status_code == 404
