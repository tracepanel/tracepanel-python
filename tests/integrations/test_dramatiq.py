import logging
from collections.abc import Generator

import pytest
from dirty_equals import IsInt, IsPartialDict, IsStr

dramatiq = pytest.importorskip("dramatiq")

from dramatiq.brokers.stub import StubBroker  # noqa: E402 (optional test group)
from dramatiq.worker import Worker  # noqa: E402

from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Link, Trace  # noqa: E402
from tracepanel.integrations import dramatiq as integration  # noqa: E402

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
    monkeypatch.setenv("TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    @dramatiq.actor
    def housekeeping() -> None:
        pass

    housekeeping.send()
    run(broker)

    assert [trace for trace in traces if trace.kind == "task"] == []
