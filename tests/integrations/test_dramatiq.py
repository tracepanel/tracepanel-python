import logging
import os
import socket
import threading
from collections.abc import Generator

import pytest
from dirty_equals import IsInstance, IsInt, IsPartialDict, IsPositiveInt, IsStr

dramatiq = pytest.importorskip("dramatiq")

from dramatiq.brokers.stub import StubBroker  # noqa: E402 (optional test group)
from dramatiq.worker import Worker  # noqa: E402

from tests.agentstore import AgentStore  # noqa: E402
from tests.tasklive import Recorder, heartbeats, live, live_when, named, setenv, without_heartbeats  # noqa: E402
from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Link, Trace  # noqa: E402
from tracepanel.integrations import dramatiq as integration  # noqa: E402
from tracepanel.storage import agent, transport  # noqa: E402
from tracepanel.storage.transport import set_writer  # noqa: E402

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
    assert task.attrs == IsPartialDict(
        {
            "task.system": "dramatiq",
            "task.id": enqueue.attrs["task.id"],
            "task.queue": "default",
            "task.state": "SUCCESS",
            "task.trigger": "request",
            "task.queue_wait": IsInt(ge=0),
            "task.args": "(3,)",
            "task.result": "3",
        }
    )
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
    assert [[error.type for error in trace.errors] for trace in attempts] == [
        ["builtins.ConnectionError"],
        ["builtins.ConnectionError"],
    ]


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
