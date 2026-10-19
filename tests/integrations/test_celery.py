import pytest
from dirty_equals import IsInt, IsList, IsPartialDict, IsStr
from django.contrib.auth.models import User
from django.test import Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore, issues, newer
from tracepanel.core.model import Link, Trace
from tracepanel.storage.transport import set_writer

pytest.importorskip("celery")

from celery.contrib.testing.worker import start_worker

from tests.project import jobs


def by_kind(traces: list[Trace], kind: str) -> list[Trace]:
    return [trace for trace in traces if trace.kind == kind]


def task_attrs(trace: Trace) -> dict[str, object]:
    return {key: value for key, value in trace.attrs.items() if key.startswith("task.")}


@pytest.mark.django_db
def test_eager_task_from_a_request(
    client: Client, traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    client.get("/run-eager/")
    (task,) = by_kind(traces, "task")
    (request,) = by_kind(traces, "http")
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    assert task.name == "tests.project.jobs.count_users"
    assert task_attrs(task) == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.count_users",
        "task.id": IsStr,
        "task.queue": None,
        "task.worker": IsStr | None,
        "task.retries": 0,
        "task.eager": True,
        "task.trigger": "request",
        "task.args": "('eager',)",
        "task.kwargs": "{}",
        "task.state": "SUCCESS",
        "task.result": "'eager: 5'",
    }
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert enqueue.attrs == IsPartialDict({"task.id": task.attrs["task.id"], "task.eager": True})
    assert [span.name for span in task.spans if span.kind == "sql"] == ["SELECT COUNT auth_user"]
    validator.validate(task.to_dict())


@pytest.mark.django_db(transaction=True)
def test_queued_task_runs_in_a_worker_linked_to_its_request(
    client: Client, traces: list[Trace], users: list[User]
) -> None:
    client.get("/enqueue/")
    (request,) = by_kind(traces, "http")
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]
    with start_worker(jobs.app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        jobs.app.AsyncResult(str(enqueue.attrs["task.id"])).get(timeout=10)
    (task,) = by_kind(traces, "task")

    assert enqueue.attrs == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.count_users",
        "task.id": task.attrs["task.id"],
        "task.queue": "celery",
        "task.eta": None,
        "task.retries": 0,
        "code.origin": IsStr(regex=r"tests/project/views\.py:\d+ in enqueue"),
        "messaging.system": "memory",
    }
    assert task.attrs["messaging.system"] == "memory"
    assert task_attrs(task) == IsPartialDict(
        {
            "task.queue": "celery",
            "task.eager": False,
            "task.trigger": "request",
            "task.queue_wait": IsInt(ge=0),
            "task.state": "SUCCESS",
            "task.result": "'queued: 5'",
        }
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


@pytest.mark.django_db
def test_failed_task(traces: list[Trace]) -> None:
    jobs.broken.apply()
    (task,) = traces

    assert (task.status, task.attrs["task.state"]) == ("error", "FAILURE")
    assert [(error.type, error.message, error.handled) for error in task.errors] == [
        ("builtins.ValueError", "boom", False)
    ]
    assert [issue.kind for issue in issues(task)] == ["error"]


@pytest.mark.django_db
def test_retried_task_is_one_trace_per_attempt(traces: list[Trace]) -> None:
    jobs.flaky.apply()

    assert [(trace.attrs["task.retries"], trace.attrs["task.state"], trace.status) for trace in traces] == IsList(
        (1, "SUCCESS", "ok"), (0, "RETRY", "ok"), check_order=False
    )
    (first,) = [trace for trace in traces if trace.attrs["task.retries"] == 0]
    assert first.attrs["task.retry_reason"] == "ConnectionError('CRM down')"
    assert [(error.type, error.handled) for error in first.errors] == [("builtins.ConnectionError", True)]


@pytest.mark.django_db
def test_issues_inside_a_task(traces: list[Trace], users: list[User]) -> None:
    jobs.groups_per_user.apply()
    (task,) = traces

    assert [issue.kind for issue in issues(task)] == ["n+1"]


@pytest.mark.django_db
def test_arguments_can_be_left_out(traces: list[Trace], users: list[User], settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_task_args": False}

    jobs.count_users.apply(("secret",))
    (task,) = traces

    assert {"task.args", "task.kwargs", "task.result"} & task.attrs.keys() == set()


@pytest.mark.django_db
def test_tasks_listing_and_links(client: Client, users: list[User], store: AgentStore) -> None:
    writer = store.writer()
    set_writer(writer)
    try:
        client.get("/run-eager/")
        jobs.broken.apply()
        writer.flush()
    finally:
        set_writer(None)
    (request,) = store.search_traces("kind:http")[0]
    (task,) = store.search_traces("task:tests.project.jobs.count_users")[0]

    assert store.list_tasks() == IsList(
        IsPartialDict(task="tests.project.jobs.count_users", runs=1, failures=0, retries=0, queues=[], wait_p50=None),
        IsPartialDict(task="tests.project.jobs.broken", runs=1, failures=1),
        check_order=False,
    )
    assert store.list_tasks("state:failure") == [IsPartialDict(task="tests.project.jobs.broken")]
    by_request = store.related(str(request["id"]))
    by_task = store.related(str(task["id"]))
    # Calls served in other projects (an agent after 0.2.0) and the calls' whole traces (after 0.3.4): none here.
    assert by_request == newer(
        {
            "enqueued_by": None,
            "enqueued": [IsPartialDict(id=task["id"])],
            "connection": None,
            "connection_events": [],
            "sent_by": None,
            "delivered": [],
            "called_by": None,
            "calls": [],
            "via": {task["id"]: IsStr(min_length=26, max_length=26)},
        },
        by_request,
        {"remote_calls": [], "call_traces": {}},
    )
    assert by_task == newer(
        {
            "enqueued_by": IsPartialDict(id=request["id"]),
            "enqueued": [],
            "connection": None,
            "connection_events": [],
            "sent_by": None,
            "delivered": [],
            "called_by": None,
            "calls": [],
            "via": {},
        },
        by_task,
        {"remote_calls": [], "call_traces": {}},
    )
    assert client.get(f"/__tracepanel__/api/traces/{task['id']}/related").json() == IsPartialDict(
        enqueued_by=IsPartialDict(id=request["id"])
    )


@pytest.mark.django_db
def test_celery_bookkeeping_logs_stay_out(traces: list[Trace], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        jobs.broken.apply()
    (task,) = traces

    assert [log.logger for log in task.logs if log.logger.startswith("celery.")] == []


@pytest.mark.django_db
def test_ignored_tasks(traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"ignore_tasks": ["tests.project.jobs.broken"]}

    jobs.broken.apply()

    assert traces == []
