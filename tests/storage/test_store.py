import pytest
from django.contrib.auth.models import User
from django.test import Client

from tracepanel.core.context import start_trace
from tracepanel.core.model import Issue
from tracepanel.storage.store import Store
from tracepanel.storage.transport import StoreWriter, set_writer

pytestmark = pytest.mark.django_db


def test_api_lists_and_reads_traces(client: Client, users: list[User], store: Store) -> None:
    writer = StoreWriter(store)
    set_writer(writer)
    try:
        trace_id = client.get("/n-plus-one/")["Tracepanel-Id"]
        writer.flush()
    finally:
        set_writer(None)

    listing = client.get("/__tracepanel__/api/traces").json()["traces"]
    assert listing[0]["id"] == trace_id
    assert listing[0]["issues"] >= 2
    assert listing[0]["method"] == "GET"
    assert listing[0]["path"] == "/n-plus-one/"
    assert listing[0]["issue_kinds"] == ["n+1"]

    detail = client.get(f"/__tracepanel__/api/traces/{trace_id}").json()
    assert detail["id"] == trace_id
    assert detail["spans"]

    issues = client.get("/__tracepanel__/api/issues").json()["issues"]
    assert {i["kind"] for i in issues} == {"n+1"}
    assert all(i["last_trace_id"] == trace_id for i in issues)

    assert client.get("/__tracepanel__/api/traces/01ARZ3NDEKTSV4RRFFQ69G5FAV").status_code == 404
    assert client.get("/__tracepanel__/api/traces?limit=oops").status_code == 200


def test_issue_occurrences_accumulate_and_old_traces_are_pruned(store: Store) -> None:
    written: list[str] = []
    for _ in range(7):
        with start_trace("http", "GET /") as trace:
            pass
        trace.issues = [Issue("0123456789abcdef", 1, "duplicates", "t", None, None, 2, [])]
        store.write([trace])
        written.append(trace.id)

    assert [t["id"] for t in store.list_traces()] == written[::-1][:5]
    (issue,) = store.list_issues()
    assert issue["occurrences"] == 7
    assert issue["last_trace_id"] == written[-1]
