import logging
import os
import socket
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from dirty_equals import IsInstance, IsInt, IsList, IsPartialDict, IsPositiveInt, IsStr

dramatiq = pytest.importorskip("dramatiq")

from dramatiq import group, pipeline  # noqa: E402 (optional test group)
from dramatiq.brokers.stub import StubBroker  # noqa: E402
from dramatiq.middleware import GroupCallbacks  # noqa: E402
from dramatiq.rate_limits.backends import StubBackend  # noqa: E402
from dramatiq.results import Results  # noqa: E402
from dramatiq.results.backends import StubBackend as StubResults  # noqa: E402
from dramatiq.worker import Worker  # noqa: E402

from tests.agentstore import AgentStore  # noqa: E402
from tests.tasklive import Recorder, heartbeats, live, live_when, named, setenv, without_heartbeats  # noqa: E402
from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Link, Span, Trace  # noqa: E402
from tracepanel.integrations import dramatiq as integration  # noqa: E402
from tracepanel.storage import agent, transport  # noqa: E402
from tracepanel.storage.transport import complete, set_writer  # noqa: E402

integration.install()
logger = logging.getLogger("tests.jobs")


@pytest.fixture
def broker() -> Generator[StubBroker, None, None]:
    stub = StubBroker()
    stub.emit_after("process_boot")
    dramatiq.set_broker(stub)
    yield stub
    stub.flush_all()
    stub.close()


def tasks(traces: list[Trace]) -> list[Trace]:
    return [trace for trace in traces if trace.kind == "task"]


def enqueues(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "task.enqueue"]


def workflow(trace: Trace) -> dict[str, object]:
    return {key: trace.attrs[key] for key in ("task.parent_id", "task.root_id", "task.group_id") if key in trace.attrs}


def run(broker: StubBroker) -> None:
    worker = Worker(broker, worker_timeout=50)
    worker.start()
    try:
        broker.join("default", fail_fast=False)
        worker.join()
    finally:
        worker.stop()


def test_send_and_run_are_linked(broker: StubBroker, traces: list[Trace]) -> None:
    @dramatiq.actor
    def count_users(limit: int) -> int:
        logger.warning("counting up to %s", limit)
        return limit

    with start_trace("http", "POST /reports/") as request:
        count_users.send(3)
    run(broker)
    (task,) = [trace for trace in traces if trace.kind == "task"]
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    assert (task.name, enqueue.name) == (
        IsStr(regex=r"tests\.integrations\.test_dramatiq\..*count_users"),
        IsStr(regex=r"enqueue tests\.integrations\.test_dramatiq\..*count_users"),
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert {key: value for key, value in task.attrs.items() if key.startswith("task.")} == {
        "task.system": "dramatiq",
        "task.name": IsStr(regex=r"tests\.integrations\.test_dramatiq\..*count_users"),
        "task.id": enqueue.attrs["task.id"],
        "task.queue": "default",
        "task.worker": WORKER,
        "task.retries": 0,
        "task.eager": False,
        "task.trigger": "request",
        "task.priority": 0,
        "task.queue_wait": IsInt(ge=0),
        "task.args": "(3,)",
        "task.kwargs": "{}",
        "task.state": "SUCCESS",
        "task.result": "3",
    }
    assert [log.message for log in task.logs] == ["counting up to 3"]


def test_failure_after_the_last_retry(broker: StubBroker, traces: list[Trace]) -> None:
    @dramatiq.actor(max_retries=1, min_backoff=1, max_backoff=1)
    def sync_crm() -> None:
        message = "CRM is down"
        raise ConnectionError(message)

    sync_crm.send()
    run(broker)
    # The retry can finish (on another worker thread) before the first attempt's trace is closed.
    tasks = [trace for trace in traces if trace.kind == "task"]
    attempts = sorted(tasks, key=lambda trace: str(trace.attrs["task.retries"]))

    assert [(trace.attrs["task.state"], trace.attrs["task.retries"], trace.status) for trace in attempts] == [
        ("RETRY", 0, "ok"),
        ("FAILURE", 1, "error"),
    ]
    assert [[(error.type, error.handled) for error in trace.errors] for trace in attempts] == [
        [("builtins.ConnectionError", True)],
        [("builtins.ConnectionError", False)],
    ]

    assert [trace.attrs.get("task.retry_reason") for trace in attempts] == ["ConnectionError('CRM is down')", None]

    assert attempts[1].errors[0].frames[-1].locals == {"message": "'CRM is down'"}


def test_ignored_tasks(broker: StubBroker, traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    @dramatiq.actor
    def housekeeping() -> None:
        pass

    housekeeping.send()
    run(broker)

    assert [trace for trace in traces if trace.kind == "task"] == []


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


# The worker a test's Worker speaks for: this process.
WORKER = f"dramatiq@{socket.gethostname()}:{os.getpid()}"
NAME = r"tests\.integrations\.test_dramatiq\..*count_users"


def test_a_sent_message_tells_the_agent_each_step(broker: StubBroker, recorder: Recorder) -> None:
    @dramatiq.actor
    def count_users(limit: int) -> int:
        return limit

    with start_trace("http", "POST /reports/") as request:
        message = count_users.send(3)
    run(broker)
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    assert without_heartbeats(recorder.events) == [
        {
            "event": "enqueued",
            "system": "dramatiq",
            "id": message.message_id,
            "time": IsPositiveInt,
            "name": IsStr(regex=NAME),
            "queue": "default",
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "dramatiq",
            "id": message.message_id,
            "time": IsPositiveInt,
            "name": IsStr(regex=NAME),
            "queue": "default",
            "worker": WORKER,
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
        },
        {
            "event": "finished",
            "system": "dramatiq",
            "id": message.message_id,
            "time": IsPositiveInt,
            "worker": WORKER,
            "state": "SUCCESS",
        },
    ]

    # The worker process says it is there as it boots, before it processes anything.
    assert heartbeats(recorder.events, WORKER)[0] == {
        "event": "heartbeat",
        "system": "dramatiq",
        "id": "",
        "time": IsPositiveInt,
        "worker": WORKER,
        "ids": [],
    }


def test_a_retry_waits_with_its_error_and_runs_again(broker: StubBroker, recorder: Recorder) -> None:
    @dramatiq.actor(max_retries=1, min_backoff=1, max_backoff=1)
    def sync_crm() -> None:
        message = "CRM is down"
        raise ConnectionError(message)

    message = sync_crm.send()
    run(broker)
    steps = [
        (event["event"], event.get("retries"), event.get("error"), event.get("state"))
        for event in without_heartbeats(recorder.events)
    ]

    assert steps == [
        ("enqueued", 0, None, None),
        ("started", 0, None, None),
        # Retries sends it again (to wait out its backoff), then the retry is told, before the next attempt starts.
        ("enqueued", 1, None, None),
        ("retried", 1, "ConnectionError('CRM is down')", None),
        ("started", 1, None, None),
        ("finished", None, None, "FAILURE"),
    ]

    assert {event["id"] for event in recorder.events if event["id"]} == {message.message_id}


def test_ignored_messages_send_no_events(
    broker: StubBroker, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    @dramatiq.actor
    def housekeeping() -> None:
        pass

    housekeeping.send()
    run(broker)

    assert without_heartbeats(recorder.events) == []


def test_arguments_are_left_out_of_the_events_too(
    broker: StubBroker, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    @dramatiq.actor
    def count_users(limit: int) -> int:
        return limit

    count_users.send(7)
    run(broker)

    assert [{"args", "kwargs"} & event.keys() for event in without_heartbeats(recorder.events)] == [set()] * 3


def test_dramatiq_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


def test_the_agent_lists_a_message_queued_then_running_until_it_ends(broker: StubBroker, store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    release = threading.Event()

    @dramatiq.actor
    def waits() -> bool:
        return release.wait(10)

    worker = Worker(broker, worker_timeout=50)
    try:
        message = waits.send()
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued == {
            "now": IsPositiveInt,
            "tasks": [IsPartialDict(id=message.message_id)],
            "workers": IsInstance(list),
        }
        assert queued["tasks"][0] == {
            "id": message.message_id,
            "system": "dramatiq",
            "name": IsStr(regex=r"tests\.integrations\.test_dramatiq\..*waits"),
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

        worker.start()
        running = live_when(store, lambda answer: answer["tasks"][0]["state"] == "running")

        assert running["tasks"] == [
            IsPartialDict(id=message.message_id, state="running", worker=WORKER, started_at=IsInt)
        ]
        assert named(running, WORKER) == [
            {"name": WORKER, "system": "dramatiq", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        release.set()
        broker.join("default")
        worker.join()
        done = live_when(store, lambda answer: not answer["tasks"])

        assert named(done, WORKER) == [IsPartialDict(name=WORKER, in_flight=0)]
    finally:
        release.set()
        worker.stop()
        set_writer(None)


def test_a_delayed_message_says_when_it_was_due_and_its_priority(broker: StubBroker, traces: list[Trace]) -> None:
    @dramatiq.actor(priority=5)
    def count_users(limit: int) -> int:
        return limit

    with start_trace("http", "POST /reports/") as request:
        sent = count_users.send_with_options(args=(3,), delay=10)
    run(broker)
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)
    due = datetime.fromtimestamp(sent.options["eta"] / 1000, tz=UTC).isoformat()

    assert enqueue.attrs == {
        "task.system": "dramatiq",
        "task.name": IsStr(regex=NAME),
        "task.id": sent.message_id,
        "task.queue": "default.DQ",
        "task.eta": due,
        "task.retries": 0,
        "task.priority": 5,
        "code.origin": IsStr,
    }
    assert task.attrs == IsPartialDict({"task.eta": due, "task.priority": 5, "task.trigger": "request"})
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


def test_a_pipeline_links_each_step_to_the_one_before(broker: StubBroker, traces: list[Trace]) -> None:
    @dramatiq.actor
    def add(a: int, b: int = 0) -> int:
        return a + b

    with start_trace("http", "POST /reports/") as request:
        pipe = pipeline([add.message(1), add.message(2), add.message(3)]).run()
    run(broker)
    runs = {trace.attrs["task.id"]: trace for trace in tasks(traces)}
    first, second, third = (runs[message.message_id] for message in pipe.messages)

    assert [workflow(step) for step in (first, second, third)] == [
        {},
        {"task.parent_id": first.attrs["task.id"], "task.root_id": first.attrs["task.id"]},
        {"task.parent_id": second.attrs["task.id"], "task.root_id": first.attrs["task.id"]},
    ]

    # Pipelines sends the next step from inside the run before it.
    assert [step.links for step in (first, second, third)] == [
        [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueues(request)[0].id)],
        [Link(kind="enqueued_by", trace_id=first.id, span_id=enqueues(first)[0].id)],
        [Link(kind="enqueued_by", trace_id=second.id, span_id=enqueues(second)[0].id)],
    ]

    # Each step gets the result before it.
    assert [step.attrs["task.args"] for step in (first, second, third)] == ["(1,)", "(2, 1)", "(3, 3)"]


def test_a_groups_messages_share_its_id_and_its_callback_follows_the_last(traces: list[Trace]) -> None:
    stub = StubBroker()
    stub.add_middleware(Results(backend=StubResults()))
    stub.add_middleware(GroupCallbacks(StubBackend()))
    stub.emit_after("process_boot")
    dramatiq.set_broker(stub)

    @dramatiq.actor
    def add(a: int) -> int:
        return a

    @dramatiq.actor
    def notify() -> None:
        pass

    try:
        members = group([add.message(1), add.message(2)])
        members.add_completion_callback(notify.message())
        with start_trace("http", "POST /reports/"):
            members.run()
        run(stub)
    finally:
        stub.flush_all()
        stub.close()
    runs = [trace for trace in tasks(traces) if trace.name.endswith("add")]
    (callback,) = [trace for trace in tasks(traces) if trace.name.endswith("notify")]
    (last,) = [run for run in runs if run.attrs["task.id"] == callback.attrs["task.parent_id"]]

    assert [workflow(member) for member in runs] == [{"task.group_id": IsStr(min_length=1)}] * 2
    assert runs[0].attrs["task.group_id"] == runs[1].attrs["task.group_id"]

    # GroupCallbacks sends it from inside the run that completes the group.
    assert workflow(callback) == {"task.parent_id": last.attrs["task.id"], "task.root_id": last.attrs["task.id"]}
    assert callback.links == [Link(kind="enqueued_by", trace_id=last.id, span_id=enqueues(last)[0].id)]


def test_an_on_success_callback_follows_its_message(broker: StubBroker, traces: list[Trace]) -> None:
    @dramatiq.actor
    def notify(message: dict[str, object], result: int) -> None:
        pass

    @dramatiq.actor(on_success="notify")
    def count_users(limit: int) -> int:
        return limit

    sent = count_users.send(3)
    run(broker)
    (job,) = [trace for trace in tasks(traces) if trace.name.endswith("count_users")]
    (callback,) = [trace for trace in tasks(traces) if trace.name.endswith("notify")]

    assert workflow(callback) == {"task.parent_id": sent.message_id, "task.root_id": sent.message_id}
    assert callback.links == [Link(kind="enqueued_by", trace_id=job.id, span_id=enqueues(job)[0].id)]
    assert callback.attrs["task.trigger"] == "task"


def test_a_message_stopped_by_its_time_limit_says_so(broker: StubBroker, recorder: Recorder) -> None:
    @dramatiq.actor(time_limit=100, max_retries=0)
    def overruns() -> None:
        # Short sleeps: TimeLimit raises in the thread between them.
        for _ in range(500):
            time.sleep(0.01)

    sent = overruns.send()
    run(broker)
    (task,) = tasks(recorder.traces)

    assert task.attrs == IsPartialDict({"task.state": "FAILURE", "task.timeout": 0.1})
    assert [(error.type, error.handled) for error in task.errors] == [
        ("dramatiq.middleware.time_limit.TimeLimitExceeded", False)
    ]

    assert without_heartbeats(recorder.events)[-1] == {
        "event": "finished",
        "system": "dramatiq",
        "id": sent.message_id,
        "time": IsPositiveInt,
        "worker": WORKER,
        "state": "FAILURE",
    }


def test_arguments_can_be_left_out(broker: StubBroker, traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    @dramatiq.actor
    def count_users(limit: int) -> int:
        return limit

    count_users.send(7)
    run(broker)
    (task,) = tasks(traces)

    assert {"task.args", "task.kwargs", "task.result"} & task.attrs.keys() == set()


def test_tasks_listing_and_links(broker: StubBroker, store: AgentStore) -> None:
    @dramatiq.actor
    def count_users(limit: int) -> int:
        return limit

    @dramatiq.actor(max_retries=0)
    def sync_crm() -> None:
        message = "CRM is down"
        raise ConnectionError(message)

    writer = store.writer()
    set_writer(writer)
    try:
        with start_trace("http", "POST /reports/") as request:
            count_users.send(3)
            sync_crm.send()
        # A request's trace is written as it ends.
        complete(request)
        run(broker)
        writer.flush()
    finally:
        set_writer(None)
    (runs, _) = store.search_traces("kind:task")
    (counted,) = [found for found in runs if str(found["name"]).endswith("count_users")]

    assert store.list_tasks() == IsList(
        IsPartialDict(task=IsStr(regex=NAME), system="dramatiq", runs=1, failures=0),
        IsPartialDict(task=IsStr(regex=r".*sync_crm"), system="dramatiq", runs=1, failures=1),
        check_order=False,
    )
    assert store.related(str(counted["id"]))["enqueued_by"] == IsPartialDict(id=request.id)
