import pytest

pytest.importorskip("django_tasks_db")

from typing import Any

from dirty_equals import IsInstance, IsInt, IsPartialDict, IsPositiveInt, IsStr
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore
from tests.project import django_jobs
from tests.tasklive import Recorder, live, live_when, named, without_heartbeats
from tracepanel.core.ids import new_id
from tracepanel.core.model import Link, Span, Trace
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import set_writer


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
def test_store_links_runs_to_enqueues_in_any_order(store: AgentStore, run_first: bool) -> None:  # noqa: FBT001
    run, request = _run("t-1"), _request("t-1")
    for trace in (run, request) if run_first else (request, run):
        store.write([trace])

    assert store.related(run.id)["enqueued_by"] == IsPartialDict(id=request.id)
    assert [item["id"] for item in store.related(request.id)["enqueued"]] == [run.id]
    assert store.related(request.id)["via"] == {run.id: request.spans[1].id}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


def work(worker: str = "worker-1") -> None:
    call_command(
        "db_worker",
        "--backend",
        "database",
        "--batch",
        "--no-reload",
        "--no-startup-delay",
        "--worker-id",
        worker,
        verbosity=0,
    )


@pytest.mark.django_db(transaction=True)
def test_a_queued_task_tells_the_agent_each_step(client: Client, recorder: Recorder, users: list[User]) -> None:
    client.get("/django-tasks/queued/")
    work()
    (request,) = by_kind(recorder.traces, "http")
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]
    task_id = enqueue.attrs["task.id"]

    assert without_heartbeats(recorder.events) == [
        {
            "event": "enqueued",
            "system": "django",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.project.django_jobs.queued_count",
            "queue": "default",
            "retries": 0,
            "args": "('queued',)",
            "kwargs": "{}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "django",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.project.django_jobs.queued_count",
            "queue": "default",
            "worker": "worker-1",
            "retries": 0,
            "args": "('queued',)",
            "kwargs": "{}",
        },
        {
            "event": "finished",
            "system": "django",
            "id": task_id,
            "time": IsPositiveInt,
            "worker": "worker-1",
            "state": "SUCCESS",
        },
    ]


@pytest.mark.django_db
def test_immediate_and_ignored_tasks_send_no_events(client: Client, recorder: Recorder, settings: Settings) -> None:
    # The ImmediateBackend runs them inside enqueue(): they are never in flight.
    client.get("/django-tasks/inline/")
    client.get("/django-tasks/broken/")
    settings.TRACEPANEL = {"ignore_tasks": ["tests.project.django_jobs.queued_count"]}
    django_jobs.queued_count.enqueue("ignored")

    assert recorder.events == []


@pytest.mark.django_db
def test_arguments_are_left_out_of_the_events_too(recorder: Recorder, settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_task_args": False}

    django_jobs.queued_count.enqueue("secret")
    (event,) = recorder.events

    assert {"args", "kwargs"} & event.keys() == set()


def test_django_tasks_say_in_the_hello_that_they_send_task_events() -> None:
    assert "live_tasks" in agent.SENDS


@pytest.mark.django_db(transaction=True)
def test_the_agent_lists_a_task_queued_then_running_until_it_ends(store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    seen: list[dict[str, Any]] = []
    django_jobs.during[:] = [
        lambda: seen.append(live_when(store, lambda answer: answer["tasks"][0]["state"] == "running"))
    ]
    try:
        result = django_jobs.watched.enqueue()
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued == {
            "now": IsPositiveInt,
            "tasks": [IsPartialDict(id=str(result.id))],
            "workers": IsInstance(list),
        }
        assert queued["tasks"][0] == {
            "id": str(result.id),
            "system": "django",
            "name": "tests.project.django_jobs.watched",
            "service": IsStr,
            "state": "queued",
            "queue": "default",
            "worker": None,
            "enqueued_at": IsPositiveInt,
            "started_at": None,
            "retries": 0,
            "args": "()",
            "kwargs": "{}",
            "error": None,
            "enqueued_by": None,
        }

        work("worker-live")
        (running,) = seen

        assert running["tasks"] == [
            IsPartialDict(id=str(result.id), state="running", worker="worker-live", started_at=IsInt)
        ]
        assert named(running, "worker-live") == [
            {"name": "worker-live", "system": "django", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        done = live_when(store, lambda answer: not answer["tasks"])

        assert named(done, "worker-live") == [IsPartialDict(name="worker-live", in_flight=0)]
    finally:
        django_jobs.during.clear()
        set_writer(None)
