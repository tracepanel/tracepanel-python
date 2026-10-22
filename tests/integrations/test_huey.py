import importlib
import logging
import os
import socket
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dirty_equals import IsInstance, IsInt, IsList, IsPartialDict, IsPositiveInt, IsStr

pytest.importorskip("huey")

from huey import MemoryHuey, SqliteHuey, crontab, group
from huey.api import Huey, Task
from huey.exceptions import CancelExecution
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore
from tests.tasklive import Recorder, heartbeats, live, live_when, named, setenv, without_heartbeats
from tracepanel.core.context import start_trace
from tracepanel.core.model import Link, Span, Trace
from tracepanel.integrations import huey as integration
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import complete, set_writer

integration.install()
logger = logging.getLogger("tests.jobs")
huey = MemoryHuey("tests")
# The worker a test's consumer speaks for: this process.
WORKER = f"huey@{socket.gethostname()}:{os.getpid()}"


@huey.task()
def count_users(limit: int) -> int:
    logger.warning("counting up to %s", limit)
    return limit


@huey.task(retries=1)
def sync_crm() -> None:
    message = "CRM is down"
    raise ConnectionError(message)


@huey.task()
def housekeeping() -> None:
    pass


@huey.task(priority=5)
def add(a: int, b: int = 0) -> int:
    return a + b


@huey.task()
def total(results: list[int]) -> int:
    return sum(results)


@huey.task()
def fan_out() -> str:
    # A task that enqueues another from inside its run.
    return add(1).id


@huey.task(context=True, timeout=0.05)
def overruns(task: Task) -> None:
    # Longer than its timeout: a thread worker's timeout is cooperative, the task checks it.
    time.sleep(0.1)
    task.check_timeout()


@huey.task()
def cancels() -> None:
    raise CancelExecution(retry=False)


@huey.periodic_task(crontab(minute="0", hour="3"))
def nightly() -> None:
    pass


@pytest.fixture(autouse=True)
def fresh() -> Generator[None, None, None]:
    huey.immediate = False
    huey.flush()
    yield
    huey.immediate = False
    huey.flush()


def work(instance: Huey = huey, now: datetime | None = None) -> None:
    """A consumer's worker, in the test's thread, until the queue is empty."""
    consumer = instance.create_consumer(workers=1, periodic=False, check_worker_health=False)
    ((worker, _),) = consumer.worker_threads
    worker.initialize()
    try:
        while instance.pending_count():
            worker.loop(now)
    finally:
        worker.shutdown()


def schedule(now: datetime) -> None:
    """What the consumer's scheduler does for due tasks: moves them to the queue."""
    for task in huey.read_schedule(now):
        huey.enqueue(task)


def tasks(traces: list[Trace]) -> list[Trace]:
    return [trace for trace in traces if trace.kind == "task"]


def enqueues(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "task.enqueue"]


def workflow(trace: Trace) -> dict[str, object]:
    return {key: trace.attrs[key] for key in ("task.parent_id", "task.root_id", "task.group_id") if key in trace.attrs}


def test_enqueue_and_run_are_linked(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/") as request:
        result = count_users(3)
    work()
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert (task.name, enqueue.name) == (
        "tests.integrations.test_huey.count_users",
        "enqueue tests.integrations.test_huey.count_users",
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert enqueue.attrs == {
        "task.system": "huey",
        "task.name": "tests.integrations.test_huey.count_users",
        "task.id": result.id,
        "task.queue": "tests",
        "task.eager": False,
        "code.origin": IsStr(regex=r"tests/integrations/test_huey\.py:\d+ in test_enqueue_and_run_are_linked"),
        "messaging.system": "memory",
    }
    assert {key: value for key, value in task.attrs.items() if key.startswith("task.")} == {
        "task.system": "huey",
        "task.name": "tests.integrations.test_huey.count_users",
        "task.id": result.id,
        "task.queue": "tests",
        "task.worker": WORKER,
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

    # What the run needed of its enqueue is gone from the storage.
    assert huey.storage.result_items() == {result.id: IsInstance(bytes)}


def test_retry_then_failure(traces: list[Trace]) -> None:
    sync_crm()
    work()
    attempts = tasks(traces)

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


def test_a_task_that_cancels_itself_is_revoked(traces: list[Trace]) -> None:
    cancels()
    work()
    (task,) = tasks(traces)

    assert (task.attrs["task.state"], task.status, task.errors) == ("REVOKED", "ok", [])


def test_immediate_mode_runs_in_the_caller(traces: list[Trace]) -> None:
    huey.immediate = True

    with start_trace("http", "POST /now/") as request:
        result = count_users(1)
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert enqueue.attrs == IsPartialDict({"task.id": result.id, "task.eager": True})
    assert task.attrs == IsPartialDict(
        {
            "task.id": result.id,
            "task.worker": None,
            "task.eager": True,
            "task.trigger": "request",
            "task.state": "SUCCESS",
            "service.role": "web",
        }
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


def test_ignored_tasks(traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    with start_trace("http", "POST /reports/") as request:
        housekeeping()
    work()

    assert (tasks(traces), enqueues(request)) == ([], [])


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


def test_a_queued_task_tells_the_agent_each_step(recorder: Recorder) -> None:
    with start_trace("http", "POST /reports/") as request:
        result = count_users(3)
    work()
    (enqueue,) = enqueues(request)

    assert without_heartbeats(recorder.events) == [
        {
            "event": "enqueued",
            "system": "huey",
            "id": result.id,
            "time": IsPositiveInt,
            "name": "tests.integrations.test_huey.count_users",
            "queue": "tests",
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "huey",
            "id": result.id,
            "time": IsPositiveInt,
            "name": "tests.integrations.test_huey.count_users",
            "queue": "tests",
            "worker": WORKER,
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
        },
        {
            "event": "finished",
            "system": "huey",
            "id": result.id,
            "time": IsPositiveInt,
            "worker": WORKER,
            "state": "SUCCESS",
        },
    ]

    # The worker says it is there as it starts, before it runs anything.
    assert heartbeats(recorder.events, WORKER)[0] == {
        "event": "heartbeat",
        "system": "huey",
        "id": "",
        "time": IsPositiveInt,
        "worker": WORKER,
        "ids": [],
    }


def test_a_retry_waits_with_its_error_and_runs_again(recorder: Recorder) -> None:
    result = sync_crm()
    work()
    steps = [
        (event["event"], event.get("retries"), event.get("error"), event.get("state"))
        for event in without_heartbeats(recorder.events)
    ]

    assert steps == [
        ("enqueued", 0, None, None),
        ("started", 0, None, None),
        # Huey puts it back on the queue itself: no second enqueue, the retry says it waits.
        ("retried", 1, "ConnectionError('CRM is down')", None),
        ("started", 1, None, None),
        ("finished", None, None, "FAILURE"),
    ]

    assert {event["id"] for event in recorder.events if event["id"]} == {result.id}


def test_a_delayed_retry_is_moved_to_the_queue_without_a_second_enqueue(recorder: Recorder) -> None:
    huey.enqueue(sync_crm.s(retry_delay=1))
    work()
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=2)
    schedule(later)
    work(now=later)
    attempts = tasks(recorder.traces)

    assert [(trace.attrs["task.state"], trace.attrs["task.retries"]) for trace in attempts] == [
        ("RETRY", 0),
        ("FAILURE", 1),
    ]
    assert attempts[1].attrs["task.eta"] == IsStr

    assert [event["event"] for event in without_heartbeats(recorder.events)] == [
        "enqueued",
        "started",
        "retried",
        "started",
        "finished",
    ]


def test_immediate_and_ignored_tasks_send_no_events(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")
    housekeeping()
    work()
    huey.immediate = True
    count_users(1)

    assert without_heartbeats(recorder.events) == []


def test_arguments_are_left_out_of_the_events_too(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    count_users(7)
    work()

    assert [{"args", "kwargs"} & event.keys() for event in without_heartbeats(recorder.events)] == [set()] * 3


def test_a_revoked_task_finishes_without_a_run(recorder: Recorder) -> None:
    result = count_users(7)
    result.revoke()
    work()

    assert tasks(recorder.traces) == []
    assert without_heartbeats(recorder.events)[-1] == {
        "event": "finished",
        "system": "huey",
        "id": result.id,
        "time": IsPositiveInt,
        "worker": WORKER,
        "state": "REVOKED",
    }


def test_huey_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


# The store a running task reads the agent's list from, and what it read: the worker runs it in the test's thread.
watched: list[AgentStore] = []
seen: list[dict[str, Any]] = []


@huey.task()
def look() -> None:
    (store,) = watched
    seen.append(live_when(store, lambda answer: bool(answer["tasks"]) and answer["tasks"][0]["state"] == "running"))


def test_the_agent_lists_a_task_queued_then_running_until_it_ends(store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    watched[:] = [store]
    seen.clear()
    try:
        result = look()
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued == {"now": IsPositiveInt, "tasks": [IsPartialDict(id=result.id)], "workers": IsInstance(list)}
        assert queued["tasks"][0] == {
            "id": result.id,
            "system": "huey",
            "name": "tests.integrations.test_huey.look",
            "service": IsStr,
            "state": "queued",
            "queue": "tests",
            "worker": None,
            "enqueued_at": IsPositiveInt,
            "started_at": None,
            "retries": 0,
            "args": "()",
            "kwargs": "{}",
            "error": None,
            "enqueued_by": None,
        }

        work()
        (running,) = seen

        assert running["tasks"] == [IsPartialDict(id=result.id, state="running", worker=WORKER, started_at=IsInt)]
        assert named(running, WORKER) == [
            {"name": WORKER, "system": "huey", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        done = live_when(store, lambda answer: not answer["tasks"])

        assert named(done, WORKER) == [IsPartialDict(name=WORKER, in_flight=0)]
    finally:
        set_writer(None)


def test_a_delayed_task_says_when_it_was_due_and_its_priority(recorder: Recorder) -> None:
    due = (datetime.now(UTC) + timedelta(seconds=1)).replace(microsecond=0)
    with start_trace("http", "POST /reports/") as request:
        result = add.schedule((3,), eta=due)
    # The worker finds it not due yet and puts it on the schedule; the scheduler moves it to the queue when it is.
    work()
    # Huey keeps naive datetimes in UTC.
    later = due.replace(tzinfo=None) + timedelta(seconds=1)
    schedule(later)
    work(now=later)
    (task,) = tasks(recorder.traces)
    (enqueue,) = enqueues(request)
    when = due.isoformat()

    assert enqueue.attrs == {
        "task.system": "huey",
        "task.name": "tests.integrations.test_huey.add",
        "task.id": result.id,
        "task.queue": "tests",
        "task.eager": False,
        "task.eta": when,
        "task.priority": 5,
        "code.origin": IsStr,
        "messaging.system": "memory",
    }
    assert task.attrs == IsPartialDict(
        {"task.eta": when, "task.priority": 5, "task.trigger": "request", "task.state": "SUCCESS"}
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]

    # Moved to the queue when due, it is not enqueued again.
    assert [event["event"] for event in without_heartbeats(recorder.events)] == ["enqueued", "started", "finished"]


def test_a_pipeline_links_each_step_to_the_one_before(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/") as request:
        pipe = huey.enqueue(add.s(1).then(add, 2).then(add, 3))
    work()
    runs = {trace.attrs["task.id"]: trace for trace in tasks(traces)}
    first, second, third = (runs[result.id] for result in pipe)

    assert [workflow(step) for step in (first, second, third)] == [
        {},
        {"task.parent_id": first.attrs["task.id"], "task.root_id": first.attrs["task.id"]},
        {"task.parent_id": second.attrs["task.id"], "task.root_id": first.attrs["task.id"]},
    ]

    # Huey enqueues the next step from inside the run before it.
    assert [step.links for step in (first, second, third)] == [
        [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueues(request)[0].id)],
        [Link(kind="enqueued_by", trace_id=first.id, span_id=enqueues(first)[0].id)],
        [Link(kind="enqueued_by", trace_id=second.id, span_id=enqueues(second)[0].id)],
    ]
    assert [step.attrs["task.trigger"] for step in (first, second, third)] == ["request", "task", "task"]

    # Each step gets the result before it.
    assert [step.attrs["task.args"] for step in (first, second, third)] == ["(1,)", "(2, 1)", "(3, 3)"]


def test_a_task_enqueued_from_a_task_follows_it(traces: list[Trace]) -> None:
    outer = fan_out()
    work()
    runs = {trace.attrs["task.id"]: trace for trace in tasks(traces)}
    (inner_id,) = set(runs) - {outer.id}
    (sent,) = enqueues(runs[outer.id])

    assert workflow(runs[inner_id]) == {"task.parent_id": outer.id, "task.root_id": outer.id}
    assert runs[inner_id].links == [Link(kind="enqueued_by", trace_id=runs[outer.id].id, span_id=sent.id)]
    assert runs[inner_id].attrs["task.trigger"] == "task"


def test_a_groups_tasks_share_its_id(recorder: Recorder) -> None:
    with start_trace("http", "POST /reports/") as request:
        results = huey.enqueue(group([add.s(5), add.s(6)]))
    work()
    runs = {trace.attrs["task.id"]: trace for trace in tasks(recorder.traces)}
    spans = {span.attrs["task.id"]: span for span in enqueues(request)}
    members = [runs[result.id] for result in results]

    assert [workflow(member) for member in members] == [{"task.group_id": IsStr(min_length=1)}] * 2
    assert members[0].attrs["task.group_id"] == members[1].attrs["task.group_id"]

    # One enqueue span per task, the run linked to its own.
    assert [member.links for member in members] == [
        [Link(kind="enqueued_by", trace_id=request.id, span_id=spans[result.id].id)] for result in results
    ]


def test_a_chords_callback_follows_the_last_member(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/"):
        chord = huey.enqueue(group([add.s(1), add.s(2)]).then(total))
    work()
    runs = {trace.attrs["task.id"]: trace for trace in tasks(traces)}
    members = [runs[result.id] for result in chord.results]
    callback = runs[chord.callback.id]

    assert [workflow(member) for member in members] == [{"task.group_id": IsStr(min_length=1)}] * 2
    assert members[0].attrs["task.group_id"] == members[1].attrs["task.group_id"]

    # Huey enqueues it from inside the run that completes the chord.
    last = members[-1]
    assert workflow(callback) == {"task.parent_id": last.attrs["task.id"], "task.root_id": last.attrs["task.id"]}
    assert callback.links == [Link(kind="enqueued_by", trace_id=last.id, span_id=enqueues(last)[0].id)]
    assert callback.attrs["task.result"] == "3"


def test_a_periodic_task_runs_on_schedule(traces: list[Trace]) -> None:
    at = datetime(2026, 10, 22, 3, 0)  # noqa: DTZ001 (Huey's naive UTC)
    for task in huey.read_periodic(at):
        huey.enqueue(task)
    work()
    (task,) = tasks(traces)

    assert task.attrs == IsPartialDict(
        {"task.name": "tests.integrations.test_huey.nightly", "task.trigger": "schedule", "task.state": "SUCCESS"}
    )


def test_a_task_stopped_by_its_timeout_says_so(recorder: Recorder) -> None:
    result = overruns()
    work()
    (task,) = tasks(recorder.traces)

    assert task.attrs == IsPartialDict({"task.state": "FAILURE", "task.timeout": 0.05})
    assert [(error.type, error.handled) for error in task.errors] == [("huey.exceptions.TaskTimeout", False)]

    assert without_heartbeats(recorder.events)[-1] == {
        "event": "finished",
        "system": "huey",
        "id": result.id,
        "time": IsPositiveInt,
        "worker": WORKER,
        "state": "FAILURE",
    }


def test_arguments_can_be_left_out(traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    count_users(7)
    work()
    (task,) = tasks(traces)

    assert {"task.args", "task.kwargs", "task.result"} & task.attrs.keys() == set()


def test_sqlite_storage(tmp_path: Path, traces: list[Trace]) -> None:
    sqlite = SqliteHuey("sqlite", filename=str(tmp_path / "huey.db"))

    @sqlite.task()
    def double(value: int) -> int:
        return value * 2

    with start_trace("http", "POST /reports/") as request:
        result = double(4)
    work(sqlite)
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert task.attrs == IsPartialDict(
        {"task.id": result.id, "task.queue": "sqlite", "messaging.system": "sqlite", "task.result": "8"}
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


@pytest.fixture
def djhuey(settings: Settings) -> ModuleType:
    """The instance of huey.contrib.djhuey: it reads settings.HUEY as it is imported, once."""
    settings.HUEY = {"huey_class": "huey.MemoryHuey", "name": "django", "immediate": False}
    module = importlib.import_module("huey.contrib.djhuey")
    module.HUEY.flush()
    return module


@pytest.mark.django_db
def test_django_huey(djhuey: ModuleType, traces: list[Trace]) -> None:
    @djhuey.db_task()
    def send_digest(user_id: int) -> int:
        return user_id

    with start_trace("http", "POST /digest/") as request:
        result = send_digest(7)
    work(djhuey.HUEY)
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert task.attrs == IsPartialDict(
        {"task.system": "huey", "task.id": result.id, "task.queue": "django", "task.state": "SUCCESS"}
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


@pytest.mark.django_db
def test_django_tasks_on_huey_are_traced_once(djhuey: ModuleType, settings: Settings, traces: list[Trace]) -> None:
    pytest.importorskip("django_tasks_db")
    from tests.project import django_jobs  # noqa: PLC0415 (Django Tasks are optional)

    settings.TASKS = {**settings.TASKS, "huey": {"BACKEND": "huey.contrib.djhuey.tasks_backend.HueyBackend"}}

    with start_trace("http", "POST /digest/") as request:
        django_jobs.count_users.using(backend="huey").enqueue("huey")
    work(djhuey.HUEY)
    (task,) = tasks(traces)

    # The Django Tasks integration traces them: Huey's task that runs them is left alone.
    assert task.attrs == IsPartialDict(
        {"task.system": "django", "task.name": "tests.project.django_jobs.count_users", "task.state": "SUCCESS"}
    )
    assert [span.attrs["task.system"] for span in enqueues(request)] == ["django"]


def test_tasks_listing_and_links(store: AgentStore) -> None:
    writer = store.writer()
    set_writer(writer)
    try:
        with start_trace("http", "POST /reports/") as request:
            count_users(3)
            huey.enqueue(sync_crm.s(retries=0))
        # A request's trace is written as it ends.
        complete(request)
        work()
        writer.flush()
    finally:
        set_writer(None)
    (runs, _) = store.search_traces("task:tests.integrations.test_huey.count_users")

    assert store.list_tasks() == IsList(
        IsPartialDict(task="tests.integrations.test_huey.count_users", system="huey", runs=1, failures=0),
        IsPartialDict(task="tests.integrations.test_huey.sync_crm", system="huey", runs=1, failures=1),
        check_order=False,
    )
    assert store.related(str(runs[0]["id"]))["enqueued_by"] == IsPartialDict(id=request.id)
