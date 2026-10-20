import logging
from collections.abc import Generator
from typing import Any

import pytest
from dirty_equals import IsInt, IsPartialDict, IsPositiveInt, IsStr

pytest.importorskip("rq")
fakeredis = pytest.importorskip("fakeredis")

from rq import Queue, Retry, SimpleWorker  # noqa: E402 (optional test group)

from tests.agentstore import AgentStore  # noqa: E402
from tests.tasklive import Recorder, heartbeats, live, live_when, without_heartbeats  # noqa: E402
from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Link, Trace  # noqa: E402
from tracepanel.integrations import rq as integration  # noqa: E402
from tracepanel.storage import agent, transport  # noqa: E402
from tracepanel.storage.transport import set_writer  # noqa: E402

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


def work(queue: Queue) -> SimpleWorker:
    worker = SimpleWorker([queue], connection=queue.connection)
    worker.work(burst=True)
    return worker


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


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


def test_a_queued_job_tells_the_agent_each_step(queue: Queue, recorder: Recorder) -> None:
    with start_trace("http", "POST /reports/") as request:
        job = queue.enqueue(count_users, 3)
    worker = work(queue)
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    assert without_heartbeats(recorder.events) == [
        {
            "event": "enqueued",
            "system": "rq",
            "id": job.id,
            "time": IsPositiveInt,
            "name": "tests.integrations.test_rq.count_users",
            "queue": "default",
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "rq",
            "id": job.id,
            "time": IsPositiveInt,
            "name": "tests.integrations.test_rq.count_users",
            "queue": "default",
            "worker": worker.name,
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
        },
        {
            "event": "finished",
            "system": "rq",
            "id": job.id,
            "time": IsPositiveInt,
            "worker": worker.name,
            "state": "SUCCESS",
        },
    ]

    # The worker says it is there as it starts, before it runs anything.
    assert heartbeats(recorder.events)[0] == {
        "event": "heartbeat",
        "system": "rq",
        "id": "",
        "time": IsPositiveInt,
        "worker": worker.name,
        "ids": [],
    }


def test_a_retry_waits_with_its_error_and_runs_again(queue: Queue, recorder: Recorder) -> None:
    job = queue.enqueue(sync_crm, retry=Retry(max=1))
    work(queue)
    steps = [
        (event["event"], event.get("retries"), event.get("error"), event.get("state"))
        for event in without_heartbeats(recorder.events)
    ]

    assert steps == [
        ("enqueued", 0, None, None),
        ("started", 0, None, None),
        # RQ puts it back on the queue itself: no second enqueue, the retry says it waits.
        ("retried", 1, "ConnectionError('CRM is down')", None),
        ("started", 1, None, None),
        ("finished", None, None, "FAILURE"),
    ]

    assert {event["id"] for event in recorder.events if event["id"]} == {job.id}


def test_inline_and_ignored_jobs_send_no_events(
    queue: Queue, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_IGNORE_TASKS", "*.housekeeping")
    Queue("default", connection=fakeredis.FakeRedis(), is_async=False).enqueue(count_users, 1)
    queue.enqueue(housekeeping)
    work(queue)

    assert without_heartbeats(recorder.events) == []


def test_arguments_are_left_out_of_the_events_too(
    queue: Queue, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    queue.enqueue(count_users, 7)
    work(queue)

    assert [{"args", "kwargs"} & event.keys() for event in without_heartbeats(recorder.events)] == [set()] * 3


def test_rq_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


# The store a running job reads the agent's list from, and what it read: SimpleWorker runs it in the test's thread.
watched: list[AgentStore] = []
seen: list[dict[str, Any]] = []


def look() -> None:
    (store,) = watched
    seen.append(live_when(store, lambda answer: bool(answer["tasks"]) and answer["tasks"][0]["state"] == "running"))


def test_the_agent_lists_a_job_queued_then_running_until_it_ends(queue: Queue, store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    watched[:] = [store]
    seen.clear()
    try:
        job = queue.enqueue(look)
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued == {"now": IsPositiveInt, "tasks": [IsPartialDict(id=job.id)], "workers": []}
        assert queued["tasks"][0] == {
            "id": job.id,
            "system": "rq",
            "name": "tests.integrations.test_rq.look",
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

        worker = work(queue)
        (running,) = seen

        assert running["tasks"] == [IsPartialDict(id=job.id, state="running", worker=worker.name, started_at=IsInt)]
        assert running["workers"] == [
            {"name": worker.name, "system": "rq", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        done = live_when(store, lambda answer: not answer["tasks"])

        assert done["workers"] == [IsPartialDict(name=worker.name, in_flight=0)]
    finally:
        set_writer(None)
