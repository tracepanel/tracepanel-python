import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dirty_equals import IsInstance, IsInt, IsList, IsPartialDict, IsPositiveInt, IsStr
from django.contrib.auth.models import User
from django.test import Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore, issues, newer
from tests.profiles import share
from tests.tasklive import Recorder, live, live_when, named, without_worker_events
from tracepanel.core import profile
from tracepanel.core.context import current_trace, start_trace
from tracepanel.core.model import Link, Span, Trace
from tracepanel.integrations import celery as celery_integration
from tracepanel.integrations._tasks import TaskRun
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import set_writer

pytest.importorskip("celery")

from billiard.einfo import ExceptionInfo
from billiard.exceptions import SoftTimeLimitExceeded, WorkerLostError
from celery import Celery, chain, chord, group
from celery.contrib.testing.mocks import TaskMessage
from celery.contrib.testing.worker import start_worker
from celery.signals import task_postrun
from celery.worker.request import Request

from tests.project import (
    jobs,
    prefork,
    views,  # noqa: F401 (connects views.group_saved to post_save of Group)
)


@pytest.fixture(autouse=True)
def no_worker_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that starts runs Django's system checks, which ask every database for its features.

    The first worker in a process would ask the analytics database, which a test of the default one may not touch:
    whichever test starts it fails, and leaves its messages queued for the next one's worker.
    """
    monkeypatch.setenv("CELERY_SKIP_CHECKS", "1")


def by_kind(traces: list[Trace], kind: str) -> list[Trace]:
    return [trace for trace in traces if trace.kind == kind]


def task_attrs(trace: Trace) -> dict[str, object]:
    return {key: value for key, value in trace.attrs.items() if key.startswith("task.")}


def workflow(trace: Trace) -> dict[str, object]:
    return {key: trace.attrs[key] for key in ("task.parent_id", "task.root_id", "task.group_id") if key in trace.attrs}


def enqueues(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "task.enqueue"]


def run_all(*results: Any) -> None:  # noqa: ANN401 (AsyncResult, GroupResult)
    """Run what is queued in a worker until each result is ready.

    Waited for, not get(): Celery's "a get() inside a task would block" is one flag for the process. The worker sets it
    as it starts and the test worker clears it once started, but a chord's header task that runs at once (its message
    was queued already) clears it for its join and puts back what it found: set, so a get() here would raise.
    """
    with start_worker(jobs.app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        deadline = time.monotonic() + 10
        while not all(result.ready() for result in results) and time.monotonic() < deadline:
            time.sleep(0.01)


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
    assert task_attrs(task) == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.count_users",
        "task.id": enqueue.attrs["task.id"],
        "task.queue": "celery",
        "task.worker": IsStr(min_length=1),
        "task.retries": 0,
        "task.eager": False,
        "task.queue_wait": IsInt(ge=0),
        "task.trigger": "request",
        "task.args": "('queued',)",
        "task.kwargs": "{}",
        "task.priority": 0,
        "task.state": "SUCCESS",
        "task.result": "'queued: 5'",
    }
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


@pytest.mark.django_db
def test_failed_task(traces: list[Trace]) -> None:
    jobs.broken.apply()
    (task,) = traces

    assert (task.status, task.attrs["task.state"]) == ("error", "FAILURE")
    assert [(error.type, error.message, error.handled) for error in task.errors] == [
        ("builtins.ValueError", "boom", False)
    ]
    assert task.errors[0].frames[-1].locals == {"message": "'boom'"}
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


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Generator[Recorder, None, None]:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    yield recorded
    # What a test enqueued and no worker ran would run in the next test's worker.
    jobs.app.control.purge()


@pytest.mark.django_db(transaction=True)
def test_a_queued_task_tells_the_agent_each_step(client: Client, recorder: Recorder, users: list[User]) -> None:
    client.get("/enqueue/")
    (request,) = by_kind(recorder.traces, "http")
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]
    task_id = str(enqueue.attrs["task.id"])
    with start_worker(jobs.app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        jobs.app.AsyncResult(task_id).get(timeout=10)

    assert without_worker_events(recorder.events) == [
        {
            "event": "enqueued",
            "system": "celery",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.project.jobs.count_users",
            "queue": "celery",
            "retries": 0,
            "args": "('queued',)",
            "kwargs": "{}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "celery",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.project.jobs.count_users",
            "queue": "celery",
            "worker": IsStr,
            "retries": 0,
            "args": "('queued',)",
            "kwargs": "{}",
        },
        {
            "event": "finished",
            "system": "celery",
            "id": task_id,
            "time": IsPositiveInt,
            "worker": IsStr,
            "state": "SUCCESS",
        },
    ]


@pytest.mark.django_db(transaction=True)
def test_a_tasks_arguments_read_the_same_queued_running_and_in_its_trace(recorder: Recorder) -> None:
    # The worker decodes the tuple the caller passed as a list.
    passed = jobs.count_users.delay("passed")
    named = jobs.count_users.apply_async(kwargs={"prefix": "named"})
    # An app hides a secret from Celery's logs and monitors with argsrepr: it stays hidden here too.
    hidden = jobs.count_users.apply_async(("s3cret",), argsrepr="('<hidden>',)")  # type: ignore[call-arg]
    with start_worker(jobs.app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        for result in (passed, named, hidden):
            result.get(timeout=10)
    shown = {
        (event["id"], event["event"]): (event["args"], event["kwargs"])
        for event in recorder.events
        if event["event"] in {"enqueued", "started"}
    }
    traced = {
        run.attrs["task.id"]: (run.attrs["task.args"], run.attrs["task.kwargs"])
        for run in by_kind(recorder.traces, "task")
    }

    assert shown == {
        (passed.id, "enqueued"): ("('passed',)", "{}"),
        (passed.id, "started"): ("('passed',)", "{}"),
        (named.id, "enqueued"): ("()", "{'prefix': 'named'}"),
        (named.id, "started"): ("()", "{'prefix': 'named'}"),
        (hidden.id, "enqueued"): ("('<hidden>',)", "{}"),
        (hidden.id, "started"): ("('<hidden>',)", "{}"),
    }

    assert traced == {
        passed.id: ("('passed',)", "{}"),
        named.id: ("()", "{'prefix': 'named'}"),
        hidden.id: ("('<hidden>',)", "{}"),
    }


@pytest.mark.django_db(transaction=True)
def test_a_retry_waits_with_its_error_and_runs_again(recorder: Recorder) -> None:
    result = jobs.flaky.delay()
    with start_worker(jobs.app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        result.get(timeout=10)
    steps = [
        (event["event"], event.get("retries"), event.get("error"), event.get("state"))
        for event in without_worker_events(recorder.events)
    ]

    assert steps == [
        ("enqueued", 0, None, None),
        ("started", 0, None, None),
        # Celery sends the task again, then tells of the retry.
        ("enqueued", 1, None, None),
        ("retried", 1, "ConnectionError('CRM down')", None),
        ("started", 1, None, None),
        ("finished", None, None, "SUCCESS"),
    ]

    assert {event["id"] for event in recorder.events if event["id"]} == {result.id}


@pytest.mark.django_db
def test_eager_and_ignored_tasks_send_no_events(recorder: Recorder, settings: Settings) -> None:
    jobs.count_users.apply(("eager",))
    settings.TRACEPANEL = {"ignore_tasks": ["tests.project.jobs.count_users"]}
    jobs.count_users.delay("ignored")

    assert without_worker_events(recorder.events) == []


@pytest.mark.django_db
def test_arguments_are_left_out_of_the_events_too(recorder: Recorder, settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_task_args": False}

    jobs.count_users.delay("secret")
    (event,) = without_worker_events(recorder.events)

    assert {"args", "kwargs"} & event.keys() == set()


def test_celery_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


@pytest.mark.django_db(transaction=True)
def test_the_agent_lists_a_task_queued_then_running_until_it_ends(client: Client, store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    jobs.release.clear()
    try:
        result = jobs.waits.delay()
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued == {"now": IsPositiveInt, "tasks": [IsPartialDict(id=result.id)], "workers": IsInstance(list)}
        assert queued["tasks"][0] == {
            "id": result.id,
            "system": "celery",
            "name": "tests.project.jobs.waits",
            "service": IsStr,
            "state": "queued",
            "queue": "celery",
            "worker": None,
            "enqueued_at": IsPositiveInt,
            "started_at": None,
            "retries": 0,
            "args": "()",
            "kwargs": "{}",
            "error": None,
            "enqueued_by": None,
        }

        with start_worker(jobs.app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
            running = live_when(store, lambda answer: answer["tasks"][0]["state"] == "running")

            assert running["tasks"] == [IsPartialDict(id=result.id, state="running", worker=IsStr, started_at=IsInt)]
            worker = running["tasks"][0]["worker"]

            assert named(running, worker) == [
                {
                    "name": worker,
                    "system": "celery",
                    "service": IsStr,
                    "last_seen": IsPositiveInt,
                    "in_flight": 1,
                }
            ]

            jobs.release.set()
            result.get(timeout=10)
            writer.flush()
            done = live_when(store, lambda answer: not answer["tasks"])

        assert named(done, worker) == [IsPartialDict(in_flight=0)]

        assert store.list_tasks() == [IsPartialDict(task="tests.project.jobs.waits", runs=1)]
    finally:
        jobs.release.set()
        set_writer(None)


@pytest.mark.django_db(transaction=True)
def test_a_chain_links_each_step_to_the_one_before(traces: list[Trace], users: list[User]) -> None:
    with start_trace("http", "POST /reports/") as request:
        result = chain(jobs.count_users.si("a"), jobs.count_users.si("b"), jobs.count_users.si("c")).apply_async()
    run_all(result)
    # Celery sends a step's arguments on as a list.
    steps = {trace.attrs["task.args"]: trace for trace in by_kind(traces, "task")}
    first, second, third = steps["('a',)"], steps["['b']"], steps["['c']"]
    (sent,) = enqueues(request)

    assert [workflow(step) for step in (first, second, third)] == [
        {},
        {"task.parent_id": first.attrs["task.id"], "task.root_id": first.attrs["task.id"]},
        {"task.parent_id": second.attrs["task.id"], "task.root_id": first.attrs["task.id"]},
    ]

    # Each step is sent from inside the run before it: its trace is the one that enqueued it.
    assert [step.links for step in (first, second, third)] == [
        [Link(kind="enqueued_by", trace_id=request.id, span_id=sent.id)],
        [Link(kind="enqueued_by", trace_id=first.id, span_id=enqueues(first)[0].id)],
        [Link(kind="enqueued_by", trace_id=second.id, span_id=enqueues(second)[0].id)],
    ]

    assert [step.attrs["task.trigger"] for step in (first, second, third)] == ["request", "task", "task"]


@pytest.mark.django_db(transaction=True)
def test_a_group_and_a_chord_share_their_group_id(traces: list[Trace], users: list[User]) -> None:
    with start_trace("http", "POST /reports/"):
        both = group(jobs.count_users.si("g1"), jobs.count_users.si("g2")).apply_async()
        summary = chord([jobs.count_users.si("h1"), jobs.count_users.si("h2")], jobs.summarize.s()).apply_async()
    run_all(both, summary)
    runs = {str(trace.attrs["task.args"]): trace for trace in by_kind(traces, "task")}
    (callback,) = [trace for trace in by_kind(traces, "task") if trace.name == "tests.project.jobs.summarize"]
    header = {runs["('h1',)"].attrs["task.id"], runs["('h2',)"].attrs["task.id"]}

    assert workflow(runs["('g1',)"]) == {"task.group_id": IsStr(min_length=1)}
    assert workflow(runs["('g2',)"]) == {"task.group_id": runs["('g1',)"].attrs["task.group_id"]}
    assert workflow(runs["('h2',)"]) == {"task.group_id": runs["('h1',)"].attrs["task.group_id"]}

    # The chord's callback is sent by the header task that finished last, from inside its run.
    assert callback.attrs["task.parent_id"] in header
    assert workflow(callback) == {
        "task.parent_id": callback.attrs["task.parent_id"],
        "task.root_id": callback.attrs["task.parent_id"],
        "task.group_id": runs["('h1',)"].attrs["task.group_id"],
    }
    assert [link.trace_id for link in callback.links] == [
        trace.id for trace in by_kind(traces, "task") if trace.attrs["task.id"] == callback.attrs["task.parent_id"]
    ]

    assert callback.attrs["task.result"] == "'h1: 5, h2: 5'"


@pytest.mark.django_db(transaction=True)
def test_a_link_callback_and_a_task_sent_from_a_task_follow_it(traces: list[Trace], users: list[User]) -> None:
    linked = jobs.count_users.apply_async(("first",), link=jobs.count_users.si("then"))
    fanned = jobs.fan_out.delay()
    with start_worker(jobs.app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        jobs.app.AsyncResult(fanned.get(timeout=10)).get(timeout=10)
        linked.get(timeout=10)
        deadline = time.monotonic() + 10
        while len(by_kind(traces, "task")) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
    runs = {str(trace.attrs["task.args"]): trace for trace in by_kind(traces, "task")}
    parent = runs["()"]

    assert workflow(runs["['then']"]) == {
        "task.parent_id": linked.id,
        "task.root_id": linked.id,
    }
    assert workflow(runs["('inner',)"]) == {"task.parent_id": parent.attrs["task.id"], "task.root_id": fanned.id}

    assert runs["('inner',)"].links == [Link(kind="enqueued_by", trace_id=parent.id, span_id=enqueues(parent)[0].id)]


@pytest.mark.django_db(transaction=True)
def test_a_delayed_task_says_when_it_was_due_and_its_priority(traces: list[Trace], users: list[User]) -> None:
    due = datetime.now(UTC) + timedelta(milliseconds=200)
    with start_trace("http", "POST /reports/") as request:
        result = jobs.count_users.apply_async(("later",), eta=due, priority=3)
    run_all(result)
    (task,) = by_kind(traces, "task")
    (enqueue,) = enqueues(request)

    assert enqueue.attrs == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.count_users",
        "task.id": result.id,
        "task.queue": "celery",
        "task.eta": due.isoformat(),
        "task.retries": 0,
        "task.priority": 3,
        "code.origin": IsStr,
        "messaging.system": "memory",
    }
    assert task_attrs(task) == IsPartialDict({"task.eta": due.isoformat(), "task.priority": 3, "task.state": "SUCCESS"})


@pytest.mark.django_db
def test_a_task_stopped_by_its_soft_time_limit_says_so(traces: list[Trace]) -> None:
    jobs.overruns.apply()
    (task,) = traces

    assert (task.status, task.attrs["task.state"], task.attrs["task.timeout"]) == ("error", "FAILURE", 2)
    assert [(error.type, error.handled) for error in task.errors] == [
        ("billiard.exceptions.SoftTimeLimitExceeded", False)
    ]


@pytest.mark.django_db(transaction=True)
def test_a_task_stopped_by_its_time_limit_finishes_failed(recorder: Recorder) -> None:
    result = jobs.overruns.delay()
    run_all(result)
    (task,) = by_kind(recorder.traces, "task")

    assert task_attrs(task) == IsPartialDict({"task.state": "FAILURE", "task.timeout": 2})
    assert without_worker_events(recorder.events)[-1] == {
        "event": "finished",
        "system": "celery",
        "id": result.id,
        "time": IsPositiveInt,
        "worker": IsStr,
        "state": "FAILURE",
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.filterwarnings("ignore:Exception raised outside body:RuntimeWarning")  # Celery's, as expected here
def test_a_run_cut_short_outside_its_body_finishes_failed_with_what_cut_it(recorder: Recorder) -> None:
    # The soft time limit hits Celery storing STARTED, before the task's body: Celery gives task_postrun no state.
    result = jobs.cut_short.delay()
    run_all(result)
    (task,) = by_kind(recorder.traces, "task")

    assert task_attrs(task) == IsPartialDict({"task.id": result.id, "task.state": "FAILURE", "task.timeout": 3})
    assert (task.status, [(error.type, error.handled) for error in task.errors]) == (
        "error",
        [("billiard.exceptions.SoftTimeLimitExceeded", False)],
    )

    assert without_worker_events(recorder.events)[-1] == {
        "event": "finished",
        "system": "celery",
        "id": result.id,
        "time": IsPositiveInt,
        "worker": IsStr,
        "state": "FAILURE",
    }


@pytest.mark.django_db
def test_an_eager_task_whose_error_reaches_its_caller_finishes_failed(traces: list[Trace]) -> None:
    with pytest.raises(ValueError, match="boom"):
        jobs.broken.apply(throw=True)
    (task,) = traces

    assert (task.status, task.attrs["task.state"]) == ("error", "FAILURE")
    assert [(error.type, error.message, error.handled) for error in task.errors] == [
        ("builtins.ValueError", "boom", False)
    ]


def test_a_task_killed_by_its_hard_time_limit_still_has_its_trace(recorder: Recorder) -> None:
    # What the worker's main process holds of a task its pool process runs, as Celery's own tests build it.
    message = TaskMessage(
        jobs.waits.name,
        args=(),
        kwargs={},
        argsrepr="()",
        kwargsrepr="{}",
        timelimit=(5, None),
        tracepanel_trigger="request",
        tracepanel_parent="01TRACE:01SPAN",
    )
    message.delivery_info = {"exchange": "", "routing_key": "celery", "priority": 0, "redelivered": False}
    request = Request(message, app=jobs.app, task=jobs.waits, hostname="celery@test")

    request.on_timeout(soft=False, timeout=5.0)
    (task,) = recorder.traces

    assert task_attrs(task) == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.waits",
        "task.id": request.id,
        "task.queue": "celery",
        "task.worker": "celery@test",
        "task.retries": 0,
        "task.eager": False,
        "task.trigger": "request",
        "task.args": "()",
        "task.kwargs": "{}",
        "task.state": "FAILURE",
        "task.timeout": 5.0,
    }
    assert (task.status, [(error.type, error.handled) for error in task.errors]) == (
        "error",
        [("billiard.exceptions.TimeLimitExceeded", False)],
    )
    assert task.links == [Link(kind="enqueued_by", trace_id="01TRACE", span_id="01SPAN")]

    assert without_worker_events(recorder.events) == [
        {
            "event": "finished",
            "system": "celery",
            "id": request.id,
            "time": IsPositiveInt,
            "worker": "celery@test",
            "state": "FAILURE",
        }
    ]


def test_a_pool_process_that_died_leaves_its_runs_trace_to_the_main_process(recorder: Recorder) -> None:
    message = TaskMessage(jobs.waits.name, args=(), kwargs={}, argsrepr="()", kwargsrepr="{}")
    message.delivery_info = {"exchange": "", "routing_key": "celery", "priority": 0, "redelivered": False}
    request = Request(message, app=jobs.app, task=jobs.waits, hostname="celery@test")
    lost = "Worker exited prematurely: signal 9 (SIGKILL) Job: 0."
    try:
        raise WorkerLostError(lost)  # noqa: TRY301
    except WorkerLostError:
        # As billiard hands it over when the pool process is gone: wrapped, with the traceback as text.
        einfo = ExceptionInfo()

    request.on_failure(einfo, return_ok=True)
    (task,) = recorder.traces

    assert task_attrs(task) == IsPartialDict({"task.id": request.id, "task.state": "FAILURE"})
    assert "task.timeout" not in task.attrs
    assert (task.status, [(error.type, error.handled) for error in task.errors]) == (
        "error",
        [("billiard.exceptions.WorkerLostError", False)],
    )

    assert without_worker_events(recorder.events) == [
        {
            "event": "finished",
            "system": "celery",
            "id": request.id,
            "time": IsPositiveInt,
            "worker": "celery@test",
            "state": "FAILURE",
        }
    ]


def test_a_failure_of_the_run_itself_is_left_to_its_own_trace(recorder: Recorder) -> None:
    message = TaskMessage(jobs.broken.name, args=(), kwargs={})
    request = Request(message, app=jobs.app, task=jobs.broken, hostname="celery@test")
    boom = "boom"
    try:
        raise ValueError(boom)  # noqa: TRY301
    except ValueError:
        einfo = ExceptionInfo()

    request.on_failure(einfo, return_ok=True)

    assert (recorder.traces, without_worker_events(recorder.events)) == ([], [])


def cut_short_once(original: Callable[..., Any]) -> Callable[..., Any]:
    """`original`, but its first call is stopped by a soft time limit, as billiard's signal handler stops it."""
    calls = 0

    def call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SoftTimeLimitExceeded
        return original(*args, **kwargs)

    return call


class InterruptedAfterKeeping:
    """The integration's lock, but leaving it once the run is kept raises SoftTimeLimitExceeded: prerun's last step."""

    def __init__(self, lock: Any) -> None:  # noqa: ANN401
        self.lock = lock
        self.armed = True

    def __enter__(self) -> None:
        self.lock.__enter__()

    def __exit__(self, *exc_info: object) -> None:
        self.lock.__exit__(*exc_info)
        if self.armed and celery_integration._runs:  # noqa: SLF001
            self.armed = False
            raise SoftTimeLimitExceeded


@pytest.fixture
def after_postrun() -> Generator[list[Trace | None], None, None]:
    """The trace current once tracepanel's task_postrun is done, run by run: None each time, unless one leaked."""
    seen: list[Trace | None] = []

    def receiver(**kwargs: object) -> None:
        del kwargs
        seen.append(current_trace())

    task_postrun.connect(receiver, weak=False, dispatch_uid="tests.after_postrun")
    yield seen
    task_postrun.disconnect(dispatch_uid="tests.after_postrun")


def interrupt(where: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A soft time limit passing at that point of tracepanel's task_prerun, once."""
    if where == "before its trace":
        monkeypatch.setattr(celery_integration, "_scheduling", cut_short_once(celery_integration._scheduling))  # noqa: SLF001
    elif where == "as its trace begins":
        monkeypatch.setattr(profile, "reason", cut_short_once(profile.reason))
    elif where == "once its trace began":
        monkeypatch.setattr(celery_integration, "TaskRun", cut_short_once(TaskRun))
    else:
        monkeypatch.setattr(celery_integration, "_lock", InterruptedAfterKeeping(celery_integration._lock))  # noqa: SLF001


@pytest.mark.parametrize("where", ["before its trace", "as its trace begins", "once its trace began", "once kept"])
def test_a_soft_time_limit_in_prerun_leaves_nothing_behind_and_the_run_is_recorded(
    where: str, traces: list[Trace], after_postrun: list[Trace | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Celery's signal swallows the exception (it logs "Signal handler ... raised") and runs the task.
    interrupt(where, monkeypatch)
    first = jobs.hurried.apply(("first",))
    second = jobs.hurried.apply(("second",))
    cut, after = traces

    assert (current_trace(), after_postrun, celery_integration._runs) == (None, [None, None], {})  # noqa: SLF001

    # Prerun kept the run before the limit passed: its trace says all it knew. Otherwise one from what the task says.
    kept = where == "once kept"
    assert task_attrs(cut) == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.hurried",
        "task.id": first.id,
        "task.queue": None,
        "task.worker": IsStr | None,
        "task.retries": 0,
        "task.eager": True,
        **({"task.trigger": "other", "task.args": "('first',)", "task.kwargs": "{}"} if kept else {}),
        "task.state": "SUCCESS",
        "task.result": "'first'",
        "task.timeout": 0.005,
    }
    assert (cut.status, [(error.type, error.handled) for error in cut.errors]) == (
        "ok",
        [("billiard.exceptions.SoftTimeLimitExceeded", True)],
    )

    assert task_attrs(after) == IsPartialDict(
        {"task.id": second.id, "task.state": "SUCCESS", "task.args": "('second',)"}
    )
    assert (after.status, after.errors, after.links) == ("ok", [], [])


@pytest.mark.django_db(transaction=True)
def test_a_soft_time_limit_in_a_workers_prerun_still_has_the_run_recorded(
    recorder: Recorder, after_postrun: list[Trace | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(celery_integration, "_started", cut_short_once(celery_integration._started))  # noqa: SLF001
    first, second = jobs.hurried.delay("first"), jobs.hurried.delay("second")
    run_all(first, second)
    cut, after = by_kind(recorder.traces, "task")

    assert after_postrun == [None, None]

    assert task_attrs(cut) == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.hurried",
        "task.id": first.id,
        "task.queue": "celery",
        "task.worker": IsStr(min_length=1),
        "task.retries": 0,
        "task.eager": False,
        "task.state": "SUCCESS",
        "task.result": "'first'",
        "task.timeout": 0.005,
    }
    assert [(error.type, error.handled) for error in cut.errors] == [
        ("billiard.exceptions.SoftTimeLimitExceeded", True)
    ]
    assert task_attrs(after) == IsPartialDict({"task.id": second.id, "task.state": "SUCCESS", "task.queue_wait": IsInt})

    assert [
        (event["event"], event["id"])
        for event in without_worker_events(recorder.events)
        if event["event"] != "enqueued"
    ] == [("finished", first.id), ("started", second.id), ("finished", second.id)]


def test_a_soft_time_limit_in_postrun_still_writes_the_trace_and_leaves_nothing_behind(
    traces: list[Trace], after_postrun: list[Trace | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(celery_integration, "task_repr", cut_short_once(celery_integration.task_repr))
    jobs.hurried.apply(("first",))
    jobs.hurried.apply(("second",))
    cut, after = traces

    assert (current_trace(), after_postrun, celery_integration._runs) == (None, [None, None], {})  # noqa: SLF001

    assert (cut.attrs["task.state"], "task.result" in cut.attrs, cut.duration > 0) == ("SUCCESS", False, True)
    assert (after.attrs["task.state"], after.attrs["task.result"]) == ("SUCCESS", "'second'")


def nothing(*args: object) -> None:
    del args


@pytest.mark.django_db
def test_a_run_prerun_never_reached_is_recorded_from_its_end(
    traces: list[Trace], monkeypatch: pytest.MonkeyPatch
) -> None:
    # As when the soft time limit stops Celery before it sends task_prerun.
    monkeypatch.setattr(celery_integration, "_prerun", nothing)
    broken = jobs.broken.apply()
    hurried = jobs.hurried.apply(("late",))
    failed, succeeded = traces

    assert task_attrs(failed) == {
        "task.system": "celery",
        "task.name": "tests.project.jobs.broken",
        "task.id": broken.id,
        "task.queue": None,
        "task.worker": IsStr | None,
        "task.retries": 0,
        "task.eager": True,
        "task.state": "FAILURE",
    }
    assert (failed.status, [(error.type, error.handled) for error in failed.errors]) == (
        "error",
        [("builtins.ValueError", False)],
    )

    assert task_attrs(succeeded) == IsPartialDict(
        {"task.id": hurried.id, "task.state": "SUCCESS", "task.result": "'late'"}
    )
    assert (succeeded.status, succeeded.errors, current_trace()) == ("ok", [], None)


class PreforkWorker:
    """A real Celery worker with a prefork pool, in a process of its own (tests.project.prefork), and what it wrote."""

    def __init__(self, tmp_path: Path) -> None:
        self.folder = tmp_path / "broker"
        self.folder.mkdir()
        self.out = tmp_path / "written.jsonl"
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
        # Where billiard spawns its pool processes (macOS) rather than forking them, Celery's fast path needs this.
        env.update(PREFORK_DIR=str(self.folder), PREFORK_OUT=str(self.out), FORKED_BY_MULTIPROCESSING="1")
        command = [sys.executable, "-m", "celery", "-A", "tests.project.prefork", "worker", "--pool=prefork"]
        options = ["--concurrency=1", "--without-heartbeat", "--without-mingle", "--without-gossip", "-l", "warning"]
        self.process = subprocess.Popen(  # noqa: S603 (this interpreter)
            [*command, *options], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self.sender = Celery("prefork", broker="filesystem://", set_as_current=False)
        self.sender.conf.broker_transport_options = prefork.transport(self.folder)
        # The worker says it is there (its first heartbeat) once its pool runs.
        self.wait_for(lambda line: line.get("event") == "heartbeat")

    def send(self, name: str) -> str:
        return self.sender.send_task(f"tests.project.prefork.{name}").id

    def lines(self) -> list[dict[str, Any]]:
        if not self.out.exists():
            return []
        return [json.loads(line) for line in self.out.read_text().splitlines()]

    def wait_for(self, found: Callable[[dict[str, Any]], bool]) -> None:
        deadline = time.monotonic() + 30
        while not any(found(line) for line in self.lines()):
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.process.kill()
                pytest.fail(f"the worker did not get there: {self.process.communicate()[0].decode(errors='replace')}")
            time.sleep(0.05)

    def stop(self, sig: signal.Signals = signal.SIGTERM) -> None:
        """A warm shutdown (SIGTERM) or a cold one (SIGQUIT): the worker's processes have all ended after it."""
        if self.process.poll() is None:
            self.process.send_signal(sig)
        self.process.communicate(timeout=30)

    def ran(self, task_id: str) -> int:
        """The pool process that ran the task: the one that said it started."""
        return next(
            line["pid"] for line in self.lines() if line.get("event") == "started" and line.get("id") == task_id
        )

    def wait_gone(self, pid: int) -> None:
        """Until the worker has reaped its pool process `pid`: all it wrote is written."""
        deadline = time.monotonic() + 30
        while _alive(pid):
            if time.monotonic() > deadline:
                pytest.fail(f"pool process {pid} is still there")
            time.sleep(0.05)

    def of(self, task_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The traces of the task and the events that say it finished, each without the process that wrote it."""
        mine = [{k: v for k, v in line.items() if k != "pid"} for line in self.lines() if task_id in line.values()]
        finished = [{k: v for k, v in line.items() if k != "time"} for line in mine if line.get("event") == "finished"]
        return [line for line in mine if "trace" in line], finished


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def worker(tmp_path: Path, recorder: Recorder) -> Generator[PreforkWorker, None, None]:
    del recorder  # What the test sends goes to it, not to an agent.
    running = PreforkWorker(tmp_path)
    yield running
    if running.process.poll() is None:
        running.process.kill()
        running.process.communicate()


def test_a_run_killed_by_its_hard_time_limit_has_one_trace_failed(worker: PreforkWorker) -> None:
    task_id = worker.send("overstays")
    worker.wait_for(lambda line: line.get("event") == "finished" and line.get("id") == task_id)
    # The pool process, sent SIGTERM, had its chance to write once it is gone. Not a shutdown: one that lands while
    # billiard still kills that process can leave the worker hanging, with or without tracepanel.
    worker.wait_gone(worker.ran(task_id))
    traces, finished = worker.of(task_id)

    assert traces == [
        {
            "trace": task_id,
            "status": "error",
            "state": "FAILURE",
            "timeout": 1.0,
            "errors": ["billiard.exceptions.TimeLimitExceeded"],
        }
    ]

    assert finished == [{"event": "finished", "system": "celery", "id": task_id, "worker": IsStr, "state": "FAILURE"}]


def test_a_run_cut_short_by_a_cold_shutdown_has_one_trace_revoked(worker: PreforkWorker) -> None:
    task_id = worker.send("sleeps")
    worker.wait_for(lambda line: line.get("event") == "started" and line.get("id") == task_id)
    worker.stop(signal.SIGQUIT)
    traces, finished = worker.of(task_id)

    assert traces == [
        {
            "trace": task_id,
            "status": "error",
            "state": "REVOKED",
            "timeout": None,
            "errors": ["billiard.exceptions.Terminated"],
        }
    ]

    assert finished == [{"event": "finished", "system": "celery", "id": task_id, "worker": IsStr, "state": "REVOKED"}]


def test_a_worker_that_stops_says_so_from_its_main_process(worker: PreforkWorker) -> None:
    worker.stop()
    goodbyes = [line for line in worker.lines() if line.get("event") == "worker_stopped"]
    ready = next(line for line in worker.lines() if line["pid"] == worker.process.pid and line["event"] == "heartbeat")

    # Once its pool processes are gone, not from each of them: they are one worker.
    assert goodbyes == [
        {
            "pid": worker.process.pid,
            "event": "worker_stopped",
            "system": "celery",
            "id": "",
            "time": IsPositiveInt,
            "worker": ready["worker"],
        }
    ]


@pytest.mark.django_db(transaction=True)
def test_the_console_shows_a_chains_steps_as_related(store: AgentStore, users: list[User]) -> None:
    writer = store.writer()
    set_writer(writer)
    try:
        result = chain(jobs.count_users.si("a"), jobs.count_users.si("b")).apply_async()
        run_all(result)
        writer.flush()
    finally:
        set_writer(None)
    (runs, _) = store.search_traces("task:tests.project.jobs.count_users")
    first, second = sorted(runs, key=lambda run: run["start"])

    assert store.related(str(first["id"]))["enqueued"] == [IsPartialDict(id=second["id"])]
    assert store.related(str(second["id"]))["enqueued_by"] == IsPartialDict(id=first["id"])


@pytest.mark.django_db
def test_django_signals_inside_a_task_once_and_celery_s_own_not_at_all(traces: list[Trace]) -> None:
    jobs.make_group.apply()
    (task,) = traces
    spans = {span.id: span for span in task.spans}

    # Celery's signals (task_prerun, task_postrun…) are how the task's trace is made: not spans of their own.
    assert [
        (span.kind, span.name, spans[span.parent_id].name if span.parent_id else None)
        for span in task.spans
        if span.kind in {"signal", "function"}
    ] == [
        ("signal", "post_save", "tests.project.jobs.make_group"),
        ("function", "tests.project.views.group_saved", "post_save"),
    ]

    assert task.attrs["signal.unreceived"] == ["pre_save auth.Group: 1"]


def profiled(traces: list[Trace]) -> dict[str, bool]:
    """Each run, by its arguments: whether it has a profile."""
    return {str(trace.attrs["task.args"]): trace.profile is not None for trace in by_kind(traces, "task")}


def test_a_task_a_profiled_request_queues_is_profiled_too(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports", profile=True):
        asked = jobs.crunch.delay("asked")
    with start_trace("http", "GET /reports"):
        plain = jobs.crunch.delay("plain")
    run_all(asked, plain)

    assert profiled(traces) == {"('asked',)": True, "('plain',)": False}

    reasons = {
        str(trace.attrs["task.args"]): trace.spans[0].attrs.get("profile.reason") for trace in by_kind(traces, "task")
    }
    assert reasons == {"('asked',)": "parent", "('plain',)": None}


def test_a_task_a_request_asking_for_memory_queues_counts_memory_too(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports", profile="memory"):
        memory = jobs.crunch.delay("memory")
    with start_trace("http", "POST /reports", profile=True):
        time_only = jobs.crunch.delay("time")
    run_all(memory, time_only)
    runs = {str(trace.attrs["task.args"]): trace.profile or {} for trace in by_kind(traces, "task")}

    assert {args: ("memory" in found, "gc" in found) for args, found in runs.items()} == {
        "('memory',)": (True, True),
        "('time',)": (False, True),
    }


def test_an_eager_task_is_in_its_profiled_callers_profile(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports", profile=True) as request:
        jobs.crunch.apply(("eager",))

    assert profiled(traces) == {"('eager',)": False}
    assert share(request, "busy") > 0
