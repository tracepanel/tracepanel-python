import json
import time

import pytest
from django.test import Client
from pytest_django.fixtures import Settings

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


@pytest.mark.django_db
def test_writes_to_the_agent_come_from_this_machine_only(store: AgentStore, settings: Settings) -> None:
    # Through a dev server's proxy, or with DEBUG on a reachable host: another machine reads nothing in, clears nothing.
    client = Client(REMOTE_ADDR="10.0.0.5")
    body = json.dumps([page_view("01M4WPAGE00000000000000002")])

    refused = client.post("/__tracepanel__/api/ingest", data=body, content_type="application/json")
    cleared = client.delete("/__tracepanel__/api/store")

    assert (refused.status_code, refused.json()) == (403, {"error": "the console takes writes from this machine only"})
    assert cleared.status_code == 403

    settings.INTERNAL_IPS = ["10.0.0.5"]

    assert client.post("/__tracepanel__/api/ingest", data=body, content_type="application/json").status_code == 202


@pytest.mark.django_db
def test_a_write_over_the_limit_is_not_read(store: AgentStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracepanel.django.console.MAX_BODY", 10)
    body = json.dumps([page_view("01M4WPAGE00000000000000003")])

    response = Client().post("/__tracepanel__/api/ingest", data=body, content_type="application/json")

    assert (response.status_code, response.json()) == (413, {"error": "too large"})
