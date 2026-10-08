from pathlib import Path

import pytest

pytest.importorskip("django_tasks_db")

from dirty_equals import IsInt, IsPartialDict, IsStr
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from jsonschema import Draft202012Validator

from tracepanel.core.ids import new_id
from tracepanel.core.model import Link, Span, Trace
from tracepanel.storage.store import Store


def by_kind(traces: list[Trace], kind: str) -> list[Trace]:
    return [trace for trace in traces if trace.kind == kind]


@pytest.mark.django_db
def test_immediate_backend_runs_inside_the_enqueue(
    client: Client, traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    client.get("/django-tasks/inline/")
    (request,) = by_kind(traces, "http")
    (run,) = by_kind(traces, "task")
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    assert enqueue.attrs == {
        "task.system": "django",
        "task.name": "tests.project.django_jobs.count_users",
        "task.queue": "default",
        "task.priority": 0,
        "task.backend": "default",
        "code.origin": IsStr(regex=r"tests/project/views\.py:\d+ in django_task"),
        "task.id": run.attrs["task.id"],
    }
    assert run.attrs == IsPartialDict(
        {
            "task.system": "django",
            "task.name": "tests.project.django_jobs.count_users",
            "task.eager": True,
            "task.trigger": "request",
            "task.state": "SUCCESS",
            "task.result": "'inline: 5'",
            "service.role": "web",
        }
    )
    assert run.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert [span.name for span in run.spans if span.kind == "sql"] == ["SELECT COUNT auth_user"]
    validator.validate(run.to_dict())


@pytest.mark.django_db
def test_failed_task(client: Client, traces: list[Trace]) -> None:
    client.get("/django-tasks/broken/")
    (run,) = by_kind(traces, "task")

    assert (run.status, run.attrs["task.state"]) == ("error", "FAILURE")
    assert [(error.type, error.message, error.handled) for error in run.errors] == [
        ("builtins.ValueError", "bounce list unavailable", False)
    ]


@pytest.mark.django_db(transaction=True)
def test_database_worker_run_is_linked_by_task_id(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/django-tasks/queued/")
    call_command("db_worker", "--backend", "database", "--batch", "--no-reload", "--no-startup-delay", verbosity=0)
    (request,) = by_kind(traces, "http")
    (run,) = by_kind(traces, "task")
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    # No headers: the worker can't name the enqueue span; the store links them.
    assert run.links == []
    assert run.attrs == IsPartialDict(
        {
            "task.id": enqueue.attrs["task.id"],
            "task.eager": False,
            "task.backend": "database",
            "task.queue_wait": IsInt(ge=0),
            "task.state": "SUCCESS",
        }
    )
    assert enqueue.attrs["messaging.system"] == "database"


def _run(task_id: str) -> Trace:
    name = "tests.project.django_jobs.queued_count"
    trace = Trace(id=new_id(), kind="task", name=name, start=0, attrs={"task.id": task_id})
    trace.spans.append(Span(id=new_id(), parent_id=None, kind="task.run", name=trace.name, start=0))
    return trace


def _request(task_id: str) -> Trace:
    trace = Trace(id=new_id(), kind="http", name="GET /", start=0)
    trace.spans.append(Span(id=new_id(), parent_id=None, kind="http", name="GET /", start=0))
    trace.spans.append(
        Span(
            id=new_id(),
            parent_id=trace.spans[0].id,
            kind="task.enqueue",
            name="enqueue",
            start=0,
            attrs={"task.id": task_id},
        )
    )
    return trace


@pytest.mark.parametrize("run_first", [True, False])
def test_store_links_runs_to_enqueues_in_any_order(tmp_path: Path, run_first: bool) -> None:  # noqa: FBT001
    store = Store(tmp_path / "traces.sqlite3")
    run, request = _run("t-1"), _request("t-1")
    for trace in (run, request) if run_first else (request, run):
        store.write([trace])

    assert store.related(run.id)["enqueued_by"] == IsPartialDict(id=request.id)
    assert [item["id"] for item in store.related(request.id)["enqueued"]] == [run.id]
    assert store.related(request.id)["via"] == {run.id: request.spans[1].id}
