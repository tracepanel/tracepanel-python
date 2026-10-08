import logging
from collections.abc import Generator

import pytest
from dirty_equals import IsInt, IsPartialDict

pytest.importorskip("rq")
fakeredis = pytest.importorskip("fakeredis")

from rq import Queue, Retry, SimpleWorker  # noqa: E402 (optional test group)

from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Link, Trace  # noqa: E402
from tracepanel.integrations import rq as integration  # noqa: E402

integration.install()
logger = logging.getLogger("tests.jobs")


def count_users(limit: int) -> int:
    logger.warning("counting up to %s", limit)
    return limit


def sync_crm() -> None:
    message = "CRM is down"
    raise ConnectionError(message)


def housekeeping() -> None:
    pass


@pytest.fixture
def queue() -> Generator[Queue, None, None]:
    connection = fakeredis.FakeRedis()
    yield Queue("default", connection=connection)
    connection.flushall()


def work(queue: Queue) -> None:
    SimpleWorker([queue], connection=queue.connection).work(burst=True)


def test_enqueue_and_run_are_linked(queue: Queue, traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/") as request:
        job = queue.enqueue(count_users, 3)
    work(queue)
    (task,) = [trace for trace in traces if trace.kind == "task"]
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    assert (task.name, enqueue.name) == (
        "tests.integrations.test_rq.count_users",
        "enqueue tests.integrations.test_rq.count_users",
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert enqueue.attrs == IsPartialDict({"task.system": "rq", "task.id": job.id, "messaging.system": "redis"})
    assert task.attrs == IsPartialDict(
        {
            "task.system": "rq",
            "task.id": job.id,
            "task.queue": "default",
            "task.state": "SUCCESS",
            "task.trigger": "request",
            "task.eager": False,
            "task.queue_wait": IsInt(ge=0),
            "task.args": "(3,)",
            "task.result": "3",
        }
    )
    assert [log.message for log in task.logs] == ["counting up to 3"]


def test_retry_then_failure(queue: Queue, traces: list[Trace]) -> None:
    queue.enqueue(sync_crm, retry=Retry(max=1))
    work(queue)
    attempts = [trace for trace in traces if trace.kind == "task"]

    assert [(trace.attrs["task.state"], trace.attrs["task.retries"], trace.status) for trace in attempts] == [
        ("RETRY", 0, "ok"),
        ("FAILURE", 1, "error"),
    ]
    assert [[error.type for error in trace.errors] for trace in attempts] == [
        ["builtins.ConnectionError"],
        ["builtins.ConnectionError"],
    ]


def test_inline_queue_runs_in_the_caller(traces: list[Trace]) -> None:
    inline = Queue("default", connection=fakeredis.FakeRedis(), is_async=False)

    with start_trace("http", "POST /now/") as request:
        inline.enqueue(count_users, 1)
    (task,) = [trace for trace in traces if trace.kind == "task"]

    assert task.attrs == IsPartialDict({"task.eager": True, "task.state": "SUCCESS", "service.role": "web"})
    assert [(link.kind, link.trace_id) for link in task.links] == [("enqueued_by", request.id)]


def test_ignored_tasks(queue: Queue, traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    queue.enqueue(housekeeping)
    work(queue)

    assert [trace for trace in traces if trace.kind == "task"] == []
