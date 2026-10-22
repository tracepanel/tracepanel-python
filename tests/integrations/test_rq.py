import logging
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dirty_equals import IsInstance, IsInt, IsList, IsPartialDict, IsPositiveInt, IsStr

pytest.importorskip("rq")
fakeredis = pytest.importorskip("fakeredis")

from rq import (  # noqa: E402 (optional test group)
    Callback,
    Queue,
    Retry,
    SimpleWorker,
    get_current_job,
)
from rq.group import Group  # noqa: E402
from rq.job import Job  # noqa: E402
from rq.scheduler import RQScheduler  # noqa: E402

from tests.agentstore import AgentStore  # noqa: E402
from tests.tasklive import (  # noqa: E402
    GOODBYE,
    Recorder,
    heartbeats,
    left,
    live,
    live_when,
    named,
    setenv,
    stopped,
    without_worker_events,
)
from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Link, Span, Trace  # noqa: E402
from tracepanel.integrations import rq as integration  # noqa: E402
from tracepanel.storage import agent, transport  # noqa: E402
from tracepanel.storage.transport import complete, set_writer  # noqa: E402

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


def add(a: int, b: int = 0) -> int:
    return a + b


def fan_out() -> str:
    # A job that enqueues another from inside its run, on its own queue.
    job = get_current_job()
    assert job is not None
    return Queue(job.origin, connection=job.connection).enqueue(add, 1).id


def overruns() -> None:
    # Longer than its job_timeout: RQ's death penalty stops it.
    time.sleep(5)


def report(job: Job, connection: object, result: object, *args: object, **kwargs: object) -> None:
    logger.warning("job %s gave %s", job.id, result)


def alert(job: Job, connection: object, *exc_info: object) -> None:
    logger.warning("job %s failed", job.id)


@pytest.fixture
def queue() -> Generator[Queue, None, None]:
    connection = fakeredis.FakeRedis()
    yield Queue("default", connection=connection)
    connection.flushall()


def work(queue: Queue) -> SimpleWorker:
    worker = SimpleWorker([queue], connection=queue.connection)
    worker.work(burst=True)
    return worker


def schedule(queue: Queue) -> None:
    """What RQ's scheduler does for due jobs: moves them to their queue (without its lock: fakeredis has no Lua)."""
    scheduler = RQScheduler([queue], connection=queue.connection)
    scheduler.prepare_registries([queue.name])
    scheduler.enqueue_scheduled_jobs()


def tasks(traces: list[Trace]) -> list[Trace]:
    return [trace for trace in traces if trace.kind == "task"]


def enqueues(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "task.enqueue"]


def workflow(trace: Trace) -> dict[str, object]:
    keys = ("task.parent_id", "task.root_id", "task.group_id", "task.depends_on")
    return {key: trace.attrs[key] for key in keys if key in trace.attrs}


def test_enqueue_and_run_are_linked(queue: Queue, traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/") as request:
        job = queue.enqueue(count_users, 3)
    worker = work(queue)
    (task,) = [trace for trace in traces if trace.kind == "task"]
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]

    assert (task.name, enqueue.name) == (
        "tests.integrations.test_rq.count_users",
        "enqueue tests.integrations.test_rq.count_users",
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert enqueue.attrs == {
        "task.system": "rq",
        "task.name": "tests.integrations.test_rq.count_users",
        "task.id": job.id,
        "task.queue": "default",
        "task.eager": False,
        "code.origin": IsStr(regex=r"tests/integrations/test_rq\.py:\d+ in test_enqueue_and_run_are_linked"),
        "messaging.system": "redis",
        # fakeredis names its server at random.
        "server.address": IsStr,
        "server.port": 6379,
    }
    assert {key: value for key, value in task.attrs.items() if key.startswith("task.")} == {
        "task.system": "rq",
        "task.name": "tests.integrations.test_rq.count_users",
        "task.id": job.id,
        "task.queue": "default",
        "task.worker": worker.name,
        "task.retries": 0,
        "task.eager": False,
        "task.trigger": "request",
        "task.queue_wait": IsInt(ge=0),
        "task.args": "(3,)",
        "task.kwargs": "{}",
        "task.state": "SUCCESS",
        "task.result": "3",
    }
    assert [log.message for log in task.logs] == ["counting up to 3"]


def test_retry_then_failure(queue: Queue, traces: list[Trace]) -> None:
    queue.enqueue(sync_crm, retry=Retry(max=1))
    work(queue)
    attempts = [trace for trace in traces if trace.kind == "task"]

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


def test_inline_queue_runs_in_the_caller(traces: list[Trace]) -> None:
    inline = Queue("default", connection=fakeredis.FakeRedis(), is_async=False)

    with start_trace("http", "POST /now/") as request:
        inline.enqueue(count_users, 1)
    (task,) = [trace for trace in traces if trace.kind == "task"]

    assert task.attrs == IsPartialDict(
        # RQ names the worker of an inline job `sync`.
        {"task.eager": True, "task.worker": "sync", "task.state": "SUCCESS", "service.role": "web"}
    )
    assert [(link.kind, link.trace_id) for link in task.links] == [("enqueued_by", request.id)]


def test_ignored_tasks(queue: Queue, traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

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

    assert without_worker_events(recorder.events) == [
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
    assert heartbeats(recorder.events, worker.name)[0] == {
        "event": "heartbeat",
        "system": "rq",
        "id": "",
        "time": IsPositiveInt,
        "worker": worker.name,
        "ids": [],
    }

    # And that it stopped as it unregisters, after its jobs: its last word.
    assert stopped(recorder.events, worker.name) == [
        {"event": "worker_stopped", "system": "rq", "id": "", "time": IsPositiveInt, "worker": worker.name}
    ]
    assert [event["event"] for event in recorder.events if event.get("worker") == worker.name][-1] == "worker_stopped"


def test_a_retry_waits_with_its_error_and_runs_again(queue: Queue, recorder: Recorder) -> None:
    job = queue.enqueue(sync_crm, retry=Retry(max=1))
    work(queue)
    steps = [
        (event["event"], event.get("retries"), event.get("error"), event.get("state"))
        for event in without_worker_events(recorder.events)
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
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")
    Queue("default", connection=fakeredis.FakeRedis(), is_async=False).enqueue(count_users, 1)
    queue.enqueue(housekeeping)
    work(queue)

    assert without_worker_events(recorder.events) == []


def test_arguments_are_left_out_of_the_events_too(
    queue: Queue, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    queue.enqueue(count_users, 7)
    work(queue)

    assert [{"args", "kwargs"} & event.keys() for event in without_worker_events(recorder.events)] == [set()] * 3


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

        assert queued == {"now": IsPositiveInt, "tasks": [IsPartialDict(id=job.id)], "workers": IsInstance(list)}
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
        assert named(running, worker.name) == [
            {"name": worker.name, "system": "rq", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        # The burst ends: the worker says it stopped, and leaves the list.
        done = left(store, worker.name)

        assert named(done, worker.name) == ([] if GOODBYE else [IsPartialDict(name=worker.name, in_flight=0)])
    finally:
        set_writer(None)


def test_a_scheduled_job_says_when_it_was_due(queue: Queue, recorder: Recorder) -> None:
    due = datetime.now(UTC) - timedelta(seconds=1)
    with start_trace("http", "POST /reports/") as request:
        job = queue.enqueue_at(due, count_users, 3)
    (enqueue,) = enqueues(request)
    # Not in the queue yet: the scheduler moves it there when it is due.
    queued = without_worker_events(recorder.events)
    schedule(queue)
    worker = work(queue)
    (task,) = tasks(recorder.traces)

    assert enqueue.attrs == {
        "task.system": "rq",
        "task.name": "tests.integrations.test_rq.count_users",
        "task.id": job.id,
        "task.queue": "default",
        "task.eager": False,
        "task.eta": due.isoformat(),
        "code.origin": IsStr,
        "messaging.system": "redis",
        # fakeredis names its server at random.
        "server.address": IsStr,
        "server.port": 6379,
    }
    assert task.attrs == IsPartialDict(
        {"task.id": job.id, "task.eta": due.isoformat(), "task.worker": worker.name, "task.state": "SUCCESS"}
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]

    assert queued == [
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
        }
    ]


def test_a_job_that_waits_for_others_follows_them(queue: Queue, traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/") as request:
        first = queue.enqueue(add, 1)
        second = queue.enqueue(add, 2, depends_on=first)
        third = queue.enqueue(add, 3, depends_on=[second, first])
    work(queue)
    runs = {trace.attrs["task.id"]: trace for trace in tasks(traces)}

    assert [workflow(runs[job.id]) for job in (first, second, third)] == [
        {},
        {"task.parent_id": first.id, "task.root_id": first.id, "task.depends_on": [first.id]},
        {"task.parent_id": second.id, "task.root_id": first.id, "task.depends_on": [second.id, first.id]},
    ]

    # Each was enqueued by the request, which RQ ran once the jobs before were done.
    assert [[link.trace_id for link in runs[job.id].links] for job in (first, second, third)] == [[request.id]] * 3


def test_a_job_enqueued_from_a_job_follows_it(queue: Queue, traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/"):
        outer = queue.enqueue(fan_out)
    work(queue)
    runs = {trace.attrs["task.id"]: trace for trace in tasks(traces)}
    (inner_id,) = set(runs) - {outer.id}
    (sent,) = enqueues(runs[outer.id])

    assert workflow(runs[inner_id]) == {"task.parent_id": outer.id, "task.root_id": outer.id}
    assert runs[inner_id].links == [Link(kind="enqueued_by", trace_id=runs[outer.id].id, span_id=sent.id)]
    assert runs[inner_id].attrs["task.trigger"] == "task"


def test_a_groups_jobs_share_its_id(queue: Queue, recorder: Recorder) -> None:
    group = Group.create(connection=queue.connection)
    with start_trace("http", "POST /reports/") as request:
        jobs = group.enqueue_many(queue, [Queue.prepare_data(add, (5,)), Queue.prepare_data(add, (6,))])
    work(queue)
    runs = {trace.attrs["task.id"]: trace for trace in tasks(recorder.traces)}
    spans = {span.attrs["task.id"]: span for span in enqueues(request)}

    assert [workflow(runs[job.id]) for job in jobs] == [{"task.group_id": group.name}] * 2

    # One enqueue span per job, the run linked to its own.
    assert [runs[job.id].links for job in jobs] == [
        [Link(kind="enqueued_by", trace_id=request.id, span_id=spans[job.id].id)] for job in jobs
    ]

    assert [
        (event["event"], event["id"], event.get("span_id"))
        for event in without_worker_events(recorder.events)
        if event["event"] == "enqueued"
    ] == [("enqueued", job.id, spans[job.id].id) for job in jobs]


def test_callbacks_run_as_traces_linked_to_their_job(queue: Queue, traces: list[Trace]) -> None:
    done = queue.enqueue(add, 7, on_success=Callback(report))
    failed = queue.enqueue(sync_crm, on_failure=Callback(alert))
    worker = work(queue)
    runs = {trace.attrs.get("task.id"): trace for trace in tasks(traces) if "task.id" in trace.attrs}
    callbacks = {trace.attrs["task.callback"]: trace for trace in tasks(traces) if "task.callback" in trace.attrs}

    assert {key: value for key, value in callbacks["on_success"].attrs.items() if key.startswith("task.")} == {
        "task.system": "rq",
        "task.name": "tests.integrations.test_rq.report",
        "task.queue": "default",
        "task.worker": worker.name,
        "task.eager": False,
        "task.trigger": "task",
        "task.callback": "on_success",
        "task.parent_id": done.id,
        "task.root_id": done.id,
        "task.state": "SUCCESS",
    }
    assert callbacks["on_success"].links == [
        Link(kind="enqueued_by", trace_id=runs[done.id].id, span_id=runs[done.id].spans[0].id)
    ]
    assert [log.message for log in callbacks["on_success"].logs] == [f"job {done.id} gave 7"]

    assert callbacks["on_failure"].attrs == IsPartialDict(
        {"task.name": "tests.integrations.test_rq.alert", "task.parent_id": failed.id, "task.state": "SUCCESS"}
    )
    assert callbacks["on_failure"].links == [
        Link(kind="enqueued_by", trace_id=runs[failed.id].id, span_id=runs[failed.id].spans[0].id)
    ]


def test_a_job_stopped_by_its_timeout_says_so(queue: Queue, recorder: Recorder) -> None:
    job = queue.enqueue(overruns, job_timeout=1)
    worker = work(queue)
    (task,) = tasks(recorder.traces)

    assert task.attrs == IsPartialDict({"task.state": "FAILURE", "task.timeout": 1})
    assert [(error.type, error.handled) for error in task.errors] == [("rq.timeouts.JobTimeoutException", False)]

    assert without_worker_events(recorder.events)[-1] == {
        "event": "finished",
        "system": "rq",
        "id": job.id,
        "time": IsPositiveInt,
        "worker": worker.name,
        "state": "FAILURE",
    }


def test_arguments_can_be_left_out(queue: Queue, traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    queue.enqueue(count_users, 7)
    work(queue)
    (task,) = tasks(traces)

    assert {"task.args", "task.kwargs", "task.result"} & task.attrs.keys() == set()


def test_tasks_listing_and_links(queue: Queue, store: AgentStore) -> None:
    writer = store.writer()
    set_writer(writer)
    try:
        with start_trace("http", "POST /reports/") as request:
            queue.enqueue(count_users, 3)
            queue.enqueue(sync_crm)
        # A request's trace is written as it ends.
        complete(request)
        work(queue)
        writer.flush()
    finally:
        set_writer(None)
    (runs, _) = store.search_traces("task:tests.integrations.test_rq.count_users")

    assert store.list_tasks() == IsList(
        IsPartialDict(task="tests.integrations.test_rq.count_users", system="rq", runs=1, failures=0),
        IsPartialDict(task="tests.integrations.test_rq.sync_crm", system="rq", runs=1, failures=1),
        check_order=False,
    )
    assert store.related(str(runs[0]["id"]))["enqueued_by"] == IsPartialDict(id=request.id)
