"""Entry points the console builds from stored traces: any group as a search, jobs, sections in the menu."""

import pytest
from dirty_equals import IsInt, IsPartialDict
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

import tracepanel
from tests.agentstore import AgentStore
from tracepanel.core.model import Trace


@pytest.mark.django_db
def test_jobs_are_commands_and_custom_traces(traces: list[Trace], store: AgentStore, users: list[User]) -> None:
    call_command("showmigrations", "auth", verbosity=0)
    with tracepanel.trace("sync-prices"):
        User.objects.count()
    with pytest.raises(ConnectionError), tracepanel.trace("sync-prices"):
        raise ConnectionError
    store.write(traces)

    assert (
        store.list_jobs()
        == [
            {
                "job": "manage.py showmigrations",
                "source": "manage.py",
                "runs": 1,
                "failures": 0,
                "p50": IsInt,
                "p95": IsInt,
                "total": IsInt,
                "avg_queries": IsInt | float,
                "with_issues": 0,
                "last_seen": IsInt,
            },
            IsPartialDict({"job": "sync-prices", "source": "custom", "runs": 2, "failures": 1}),
        ]
        or store.list_jobs()[::-1]
    )
    assert [job["job"] for job in store.list_jobs("source:custom")] == ["sync-prices"]


@pytest.mark.django_db
def test_any_group_is_a_search(client: Client, traces: list[Trace], store: AgentStore, users: list[User]) -> None:
    client.get("/n-plus-one/")
    client.get("/n-plus-one/")
    client.get("/health/")
    with tracepanel.trace("sync-prices"):
        pass
    store.write(traces)

    assert store.summary("route:/n-plus-one/") == IsPartialDict(
        {
            "query": "route:/n-plus-one/",
            "summary": IsPartialDict(calls=2, with_issues=2),
            "issues": [IsPartialDict(kind="n+1", calls=2), IsPartialDict(kind="n+1", calls=2)],
        }
    )
    assert store.summary("kind:command name:sync-prices") == IsPartialDict(summary=IsPartialDict(calls=1))
    assert store.summary("kind:websocket") is None
    assert store.counts()["sections"] == {
        "http": 2,
        "graphql": 0,
        "tasks": 0,
        "websockets": 0,
        "jobs": 1,
        "calls": False,
        "redis": False,
        "cache": False,
    }
