import json
import time

import pytest
from django.test import Client

from tests.agentstore import AgentStore


def page_view(trace_id: str) -> dict[str, object]:
    """A page's view as tracepanel-js records it in the browser: its load and a call to the API."""
    return {
        "id": trace_id,
        "kind": "page",
        "name": "/books",
        "start": 1_000_000,
        "duration": 420_000,
        "status": "ok",
        "attrs": {"service.name": "shop-web", "service.role": "browser", "page.path": "/books"},
        "spans": [
            {
                "id": f"{trace_id}R",
                "parent_id": None,
                "kind": "page",
                "name": "/books",
                "start": 1_000_000,
                "duration": 420_000,
                "status": "ok",
                "attrs": {},
            }
        ],
        "logs": [],
        "errors": [],
        "links": [],
        "issues": [],
    }


@pytest.mark.django_db
def test_a_page_s_traces_reach_the_agent_through_the_app_s_console(store: AgentStore) -> None:
    # The page posts from the app's own origin: the console answers before Django's views, so CSRF doesn't apply.
    client = Client(enforce_csrf_checks=True)
    trace_id = "01M4WPAGE00000000000000001"

    response = client.post(
        "/__tracepanel__/api/ingest", data=json.dumps([page_view(trace_id)]), content_type="application/json"
    )

    assert response.status_code == 202
    assert response.json() == {"taken": 1}

    for _ in range(50):
        stored = client.get(f"/__tracepanel__/api/traces/{trace_id}")
        if stored.status_code == 200:
            break
        time.sleep(0.05)
    assert stored.json()["kind"] == "page"
    assert client.get("/__tracepanel__/api/pages").json()["pages"][0]["page"] == "/books"
