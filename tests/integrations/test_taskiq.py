"""Taskiq, with no broker server running.

A broker that keeps what is kicked for the test's worker, the InMemoryBroker, and a real `taskiq worker` on a folder
of messages (tests.project.taskiq_worker).
"""

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from dirty_equals import IsInt, IsList, IsPartialDict, IsPositiveInt, IsStr

pytest.importorskip("taskiq")

from taskiq import AsyncBroker, InMemoryBroker, SimpleRetryMiddleware, TaskiqScheduler
from taskiq.exceptions import NoResultError
from taskiq.message import BrokerMessage
from taskiq.receiver import Receiver
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.scheduler.scheduled_task import ScheduledTask

from tests.agentstore import AgentStore
from tests.profiles import busy, share
from tests.tasklive import Recorder, heartbeats, live, live_when, named, setenv, stopped, without_worker_events
from tracepanel.core.context import start_trace
from tracepanel.core.model import Link, Span, Trace
from tracepanel.integrations import taskiq as integration
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import complete, set_writer

integration.install()
logger = logging.getLogger("tests.jobs")
# The worker a test's receiver speaks for: this process.
WORKER = f"taskiq@{socket.gethostname()}:{os.getpid()}"


class ListBroker(AsyncBroker):
    """A broker that keeps what is kicked until the test's worker takes it: a worker's run, in the test's process."""

    def __init__(self, queue_name: str = "tests") -> None:
        super().__init__()
        self.queue_name = queue_name
        self.sent: list[bytes] = []

    async def kick(self, message: BrokerMessage) -> None:
        self.sent.append(message.message)

    async def listen(self) -> AsyncGenerator[bytes, None]:
        while self.sent:
            yield self.sent.pop(0)


broker = ListBroker()
retrying = ListBroker("retries").with_middlewares(SimpleRetryMiddleware(default_retry_count=2))
memory = InMemoryBroker(await_inplace=True)


@broker.task
def count_users(limit: int) -> int:
    logger.warning("counting up to %s", limit)
    return limit


@broker.task
async def add(a: int, b: int = 0) -> int:
    return a + b


@broker.task
async def fan_out() -> str:
    # A task that kicks another from inside its run.
    return (await add.kiq(1)).task_id


@broker.task
async def housekeeping() -> None:
    pass


@broker.task
async def skips() -> None:
    raise NoResultError


@broker.task
async def overruns() -> None:
    await asyncio.sleep(1)


@broker.task
def crunch(label: str) -> str:
    busy()
    return label


@broker.task
async def build_sales_report(label: str) -> str:
    await asyncio.sleep(0.15)
    return label


@retrying.task(retry_on_error=True)
async def sync_crm() -> None:
    message = "CRM is down"
    raise ConnectionError(message)


@memory.task
async def count_now(limit: int) -> int:
    return limit


@memory.task
def crunch_now(label: str) -> str:
    busy()
    return label


def run(main: Callable[[], Coroutine[Any, Any, object]]) -> None:
    asyncio.run(main())


async def work(instance: ListBroker = broker) -> None:
    """A worker's receiver, in the test's event loop, until nothing is left to run (retries included)."""
    receiver = Receiver(instance, executor=ThreadPoolExecutor(1), max_async_tasks=1)
    while instance.sent:
        await receiver.callback(instance.sent.pop(0))


@pytest.fixture(autouse=True)
def fresh() -> None:
    broker.sent.clear()
    retrying.sent.clear()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


def tasks(traces: list[Trace]) -> list[Trace]:
    return [trace for trace in traces if trace.kind == "task"]


def enqueues(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "task.enqueue"]


def workflow(trace: Trace) -> dict[str, object]:
    return {key: trace.attrs[key] for key in ("task.parent_id", "task.root_id", "task.group_id") if key in trace.attrs}


def test_kiq_and_run_are_linked(traces: list[Trace]) -> None:
    async def main() -> tuple[str, Trace]:
        with start_trace("http", "POST /reports/") as request:
            kicked = await count_users.kiq(3)
        await work()
        return kicked.task_id, request

    task_id, request = asyncio.run(main())
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert (task.name, enqueue.name) == (
        "tests.integrations.test_taskiq.count_users",
        "enqueue tests.integrations.test_taskiq.count_users",
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert enqueue.attrs == {
        "task.system": "taskiq",
        "task.name": "tests.integrations.test_taskiq.count_users",
        "task.id": task_id,
        "task.queue": "tests",
        "task.eager": False,
        "task.retries": 0,
        "code.origin": IsStr(regex=r"tests/integrations/test_taskiq\.py:\d+ in main"),
    }
    assert {key: value for key, value in task.attrs.items() if key.startswith("task.")} == {
        "task.system": "taskiq",
        "task.name": "tests.integrations.test_taskiq.count_users",
        "task.id": task_id,
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
    assert (task.status, task.errors) == ("ok", [])

    # The sync task runs in the receiver's thread pool, in the run's trace.
    assert [log.message for log in task.logs] == ["counting up to 3"]


def test_retry_then_failure(traces: list[Trace]) -> None:
    async def main() -> None:
        await sync_crm.kiq()
        await work(retrying)

    run(main)
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

    # The retry middleware kicks it again from inside the failed attempt: the next one links to that kick.
    (requeue,) = enqueues(attempts[0])
    assert attempts[1].links == [Link(kind="enqueued_by", trace_id=attempts[0].id, span_id=requeue.id)]
    assert requeue.attrs == IsPartialDict({"task.id": attempts[0].attrs["task.id"], "task.retries": 1})
    assert attempts[1].attrs["task.trigger"] == "other"


def test_a_task_that_skips_its_result_succeeds(traces: list[Trace]) -> None:
    async def main() -> None:
        await skips.kiq()
        await work()

    run(main)
    (task,) = tasks(traces)

    assert (task.attrs["task.state"], task.status, task.errors, "task.result" in task.attrs) == (
        "SUCCESS",
        "ok",
        [],
        False,
    )


def test_the_in_memory_broker_runs_in_the_caller(traces: list[Trace]) -> None:
    async def main() -> tuple[str, Trace]:
        with start_trace("http", "POST /now/") as request:
            kicked = await count_now.kiq(1)
        return kicked.task_id, request

    task_id, request = asyncio.run(main())
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert enqueue.attrs == {
        "task.system": "taskiq",
        "task.name": "tests.integrations.test_taskiq.count_now",
        "task.id": task_id,
        "task.queue": None,
        "task.eager": True,
        "task.retries": 0,
        "code.origin": IsStr,
        "messaging.system": "memory",
    }
    assert task.attrs == IsPartialDict(
        {
            "task.id": task_id,
            "task.worker": None,
            "task.eager": True,
            "task.trigger": "request",
            "task.state": "SUCCESS",
            "task.result": "1",
            "service.role": "web",
        }
    )
    assert "task.queue_wait" not in task.attrs
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


def test_the_in_memory_broker_that_runs_tasks_beside_the_caller_links_them_too(traces: list[Trace]) -> None:
    beside = InMemoryBroker()

    @beside.task
    async def double(value: int) -> int:
        return value * 2

    async def main() -> Trace:
        with start_trace("http", "POST /later/") as request:
            await double.kiq(4)
        await beside.wait_all()
        return request

    request = asyncio.run(main())
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert task.attrs == IsPartialDict({"task.eager": True, "task.state": "SUCCESS", "task.result": "8"})
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


def test_ignored_tasks(traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    async def main() -> Trace:
        with start_trace("http", "POST /reports/") as request:
            await housekeeping.kiq()
        await work()
        return request

    request = asyncio.run(main())

    assert (tasks(traces), enqueues(request)) == ([], [])


def test_a_kicked_task_tells_the_agent_each_step(recorder: Recorder) -> None:
    async def main() -> tuple[str, Trace]:
        broker.is_worker_process = True
        await broker.startup()
        try:
            with start_trace("http", "POST /reports/") as request:
                kicked = await count_users.kiq(3)
            await work()
        finally:
            await broker.shutdown()
            broker.is_worker_process = False
        return kicked.task_id, request

    task_id, request = asyncio.run(main())
    (enqueue,) = enqueues(request)

    assert without_worker_events(recorder.events) == [
        {
            "event": "enqueued",
            "system": "taskiq",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.integrations.test_taskiq.count_users",
            "queue": "tests",
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "taskiq",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.integrations.test_taskiq.count_users",
            "queue": "tests",
            "worker": WORKER,
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{}",
        },
        {
            "event": "finished",
            "system": "taskiq",
            "id": task_id,
            "time": IsPositiveInt,
            "worker": WORKER,
            "state": "SUCCESS",
        },
    ]

    # The worker says it is there as its broker starts, before it runs anything, and that it stopped as it shuts down.
    assert heartbeats(recorder.events, WORKER)[0] == {
        "event": "heartbeat",
        "system": "taskiq",
        "id": "",
        "time": IsPositiveInt,
        "worker": WORKER,
        "ids": [],
    }
    assert stopped(recorder.events, WORKER) == [
        {"event": "worker_stopped", "system": "taskiq", "id": "", "time": IsPositiveInt, "worker": WORKER}
    ]


def test_a_retry_waits_with_its_error_and_runs_again(recorder: Recorder) -> None:
    async def main() -> str:
        kicked = await sync_crm.kiq()
        await work(retrying)
        return kicked.task_id

    task_id = asyncio.run(main())
    steps = [
        (event["event"], event.get("retries"), event.get("error"), event.get("state"))
        for event in without_worker_events(recorder.events)
    ]

    assert steps == [
        ("enqueued", 0, None, None),
        ("started", 0, None, None),
        # The retry middleware kicks it again, then the retry is told, before the next attempt starts.
        ("enqueued", 1, None, None),
        ("retried", 1, "ConnectionError('CRM is down')", None),
        ("started", 1, None, None),
        ("finished", None, None, "FAILURE"),
    ]

    assert {event["id"] for event in recorder.events if event["id"]} == {task_id}


def test_in_memory_and_ignored_tasks_send_no_events(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    async def main() -> None:
        await housekeeping.kiq()
        await work()
        await count_now.kiq(1)

    run(main)

    assert without_worker_events(recorder.events) == []
    assert [trace.name for trace in tasks(recorder.traces)] == ["tests.integrations.test_taskiq.count_now"]


def test_arguments_can_be_left_out(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    async def main() -> None:
        await count_users.kiq(7)
        await work()

    run(main)
    (task,) = tasks(recorder.traces)

    assert {"task.args", "task.kwargs", "task.result"} & task.attrs.keys() == set()

    assert [{"args", "kwargs"} & event.keys() for event in without_worker_events(recorder.events)] == [set()] * 3


def test_taskiq_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


def test_a_delayed_task_says_when_it_was_due_and_its_priority(traces: list[Trace]) -> None:
    async def main() -> tuple[str, Trace]:
        with start_trace("http", "POST /reports/") as request:
            kicked = await add.kicker().with_labels(delay=60, priority=5).kiq(3)
        await work()
        return kicked.task_id, request

    task_id, request = asyncio.run(main())
    (task,) = tasks(traces)
    (enqueue,) = enqueues(request)
    due = enqueue.attrs["task.eta"]

    assert enqueue.attrs == {
        "task.system": "taskiq",
        "task.name": "tests.integrations.test_taskiq.add",
        "task.id": task_id,
        "task.queue": "tests",
        "task.eager": False,
        "task.retries": 0,
        "task.eta": IsStr(regex=r"\d{4}-\d\d-\d\dT.*\+00:00"),
        "task.priority": 5,
        "code.origin": IsStr,
    }
    assert task.attrs == IsPartialDict({"task.eta": due, "task.priority": 5, "task.state": "SUCCESS"})


def test_a_task_kicked_from_a_task_follows_it(traces: list[Trace]) -> None:
    async def main() -> str:
        outer = await fan_out.kiq()
        await work()
        return outer.task_id

    outer_id = asyncio.run(main())
    runs = {trace.attrs["task.id"]: trace for trace in tasks(traces)}
    (inner_id,) = set(runs) - {outer_id}
    (sent,) = enqueues(runs[outer_id])

    assert workflow(runs[inner_id]) == {"task.parent_id": outer_id, "task.root_id": outer_id}
    assert runs[inner_id].links == [Link(kind="enqueued_by", trace_id=runs[outer_id].id, span_id=sent.id)]
    assert runs[inner_id].attrs["task.trigger"] == "task"
    assert runs[outer_id].attrs["task.result"] == repr(inner_id)


def test_a_scheduled_task_runs_with_the_schedule_trigger(traces: list[Trace]) -> None:
    scheduler = TaskiqScheduler(broker, sources=[LabelScheduleSource(broker)])
    source = scheduler.sources[0]
    nightly = ScheduledTask(
        task_name=housekeeping.task_name, labels={}, args=[], kwargs={}, cron="0 3 * * *", schedule_id="nightly"
    )

    async def main() -> None:
        await scheduler.on_ready(source, nightly)
        await work()

    run(main)
    (task,) = tasks(traces)

    assert task.attrs == IsPartialDict(
        {
            "task.name": "tests.integrations.test_taskiq.housekeeping",
            "task.trigger": "schedule",
            "task.state": "SUCCESS",
        }
    )


def test_a_task_stopped_by_its_timeout_says_so(recorder: Recorder) -> None:
    async def main() -> str:
        kicked = await overruns.kicker().with_labels(timeout=0.05).kiq()
        await work()
        return kicked.task_id

    task_id = asyncio.run(main())
    (task,) = tasks(recorder.traces)

    assert task.attrs == IsPartialDict({"task.id": task_id, "task.state": "FAILURE", "task.timeout": 0.05})
    assert [(error.type, error.handled) for error in task.errors] == [("builtins.TimeoutError", False)]
    assert task.status == "error"


def test_the_agent_lists_a_task_queued_then_running_until_it_ends(store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    seen: list[dict[str, Any]] = []

    @broker.task
    def look() -> None:
        seen.append(live_when(store, lambda answer: bool(answer["tasks"]) and answer["tasks"][0]["state"] == "running"))

    try:

        async def kick() -> str:
            return (await look.kiq()).task_id

        task_id = asyncio.run(kick())
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued["tasks"] == [
            {
                "id": task_id,
                "system": "taskiq",
                "name": "tests.integrations.test_taskiq.test_the_agent_lists_a_task_queued_then_running_until_it_ends"
                ".<locals>.look",
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
        ]

        asyncio.run(work())
        (running,) = seen

        assert running["tasks"] == [IsPartialDict(id=task_id, state="running", worker=WORKER, started_at=IsInt)]
        assert named(running, WORKER) == [IsPartialDict(name=WORKER, system="taskiq", in_flight=1)]

        done = live_when(store, lambda answer: not answer["tasks"])

        assert named(done, WORKER) == [IsPartialDict(name=WORKER, in_flight=0)]
    finally:
        set_writer(None)


def test_tasks_listing_and_links(store: AgentStore) -> None:
    writer = store.writer()
    set_writer(writer)
    try:

        async def main() -> Trace:
            with start_trace("http", "POST /reports/") as request:
                await count_users.kiq(3)
            # A request's trace is written as it ends.
            complete(request)
            await work()
            return request

        request = asyncio.run(main())
        writer.flush()
    finally:
        set_writer(None)
    (runs, _) = store.search_traces("task:tests.integrations.test_taskiq.count_users")

    assert store.list_tasks() == IsList(
        IsPartialDict(task="tests.integrations.test_taskiq.count_users", system="taskiq", runs=1, failures=0)
    )
    assert store.related(str(runs[0]["id"]))["enqueued_by"] == IsPartialDict(id=request.id)


# ---------- profiles ----------


def profiled(traces: list[Trace]) -> dict[str, bool]:
    """Each run, by its arguments: whether it has a profile."""
    return {str(trace.attrs["task.args"]): trace.profile is not None for trace in tasks(traces)}


def test_a_task_a_profiled_request_kicks_is_profiled_too(traces: list[Trace]) -> None:
    async def main() -> None:
        with start_trace("http", "POST /reports", profile=True):
            await crunch.kiq("asked")
        with start_trace("http", "GET /reports"):
            await crunch.kiq("plain")
        await work()

    run(main)
    (asked,) = (trace for trace in tasks(traces) if trace.profile is not None)

    assert profiled(traces) == {"('asked',)": True, "('plain',)": False}

    # A sync task runs in the receiver's thread: the profile follows it there.
    assert share(asked, "crunch") > 0


def test_a_task_whose_message_asks_for_memory_counts_it(traces: list[Trace]) -> None:
    async def main() -> None:
        await crunch.kicker().with_labels(tracepanel_profile="memory").kiq("memory")
        await work()

    run(main)
    (task,) = tasks(traces)

    assert task.profile is not None
    assert "memory" in task.profile


def test_an_async_task_awaiting_a_sleep_is_profiled_down_to_it(traces: list[Trace]) -> None:
    async def main() -> None:
        await build_sales_report.kicker().with_labels(tracepanel_profile="1").kiq("asked")
        await work()

    run(main)
    (task,) = tasks(traces)
    profile = task.profile
    assert profile is not None
    frames = profile["frames"]
    stacks = profile["stacks"]
    assert isinstance(frames, list)
    assert isinstance(stacks, list)
    waiting = max(stacks, key=lambda stack: stack["count"])

    assert [frames[index]["function"] for index in waiting["frames"]][-2:] == ["build_sales_report", "sleep"]
    assert frames[waiting["frames"][-1]] == IsPartialDict({"module": "asyncio.tasks", "waiting": True})
    assert frames[waiting["frames"][-2]] == IsPartialDict({"in_app": True, "at_line": IsInt(gt=0)})


def test_an_in_memory_task_is_in_its_profiled_callers_profile(traces: list[Trace]) -> None:
    async def main() -> Trace:
        with start_trace("http", "POST /reports", profile=True) as request:
            await crunch_now.kiq("inline")
        return request

    request = asyncio.run(main())

    assert profiled(traces) == {"('inline',)": False}
    assert share(request, "crunch_now") > 0


# ---------- under FastAPI ----------


def test_a_fastapi_endpoint_that_kicks_a_task_links_its_run(recorder: Recorder) -> None:
    pytest.importorskip("taskiq_fastapi")
    from fastapi import FastAPI, Request  # noqa: PLC0415 (optional test group)
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from taskiq import TaskiqDepends  # noqa: PLC0415
    from taskiq_fastapi import init  # noqa: PLC0415

    import tracepanel.fastapi  # noqa: PLC0415

    app = FastAPI(debug=True)
    web = ListBroker("web")
    init(web, app)

    @web.task
    async def send_digest(user_id: int, request: Request = TaskiqDepends()) -> str:  # noqa: B008 (Taskiq's way)
        return f"{user_id} on {request.app.title}"

    @app.post("/digest/{user_id}")
    async def digest(user_id: int) -> dict[str, str]:
        return {"id": (await send_digest.kiq(user_id)).task_id}

    tracepanel.fastapi.install(app)
    with TestClient(app) as client:
        task_id = client.post("/digest/7").json()["id"]

    async def worker() -> None:
        # taskiq-fastapi starts the app's lifespan in the worker, and hands its tasks the app's request.
        web.is_worker_process = True
        await web.startup()
        try:
            await work(web)
        finally:
            await web.shutdown()

    run(worker)
    request = next(trace for trace in recorder.traces if trace.kind == "http")
    (task,) = tasks(recorder.traces)
    (enqueue,) = enqueues(request)

    assert task.attrs == IsPartialDict(
        {
            "task.id": task_id,
            "task.queue": "web",
            "task.trigger": "request",
            "task.state": "SUCCESS",
            "task.result": "'7 on FastAPI'",
            "task.args": "(7,)",
        }
    )
    assert task.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


# ---------- a real `taskiq worker` ----------


class CliWorker:
    """`taskiq worker` on tests.project.taskiq_worker's folder broker, in a process of its own, and what it wrote."""

    def __init__(self, tmp_path: Path) -> None:
        self.folder = tmp_path / "broker"
        self.folder.mkdir()
        self.out = tmp_path / "written.jsonl"
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
        env.update(TASKIQ_DIR=str(self.folder), TASKIQ_OUT=str(self.out))
        command = [sys.executable, "-m", "taskiq", "worker", "tests.project.taskiq_worker:broker"]
        options = ["--workers", "1", "--no-configure-logging", "--shutdown-timeout", "5"]
        self.process = subprocess.Popen(  # noqa: S603 (this interpreter)
            [*command, *options], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        # The worker process says it is there (its first heartbeat) once its broker starts.
        self.wait_for(lambda line: line.get("event") == "heartbeat")

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

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        self.process.communicate(timeout=30)


def test_a_taskiq_worker_runs_the_kicked_task_and_says_it_stopped(tmp_path: Path, traces: list[Trace]) -> None:
    from tests.project import taskiq_worker  # noqa: PLC0415 (its broker is the worker's)

    worker = CliWorker(tmp_path)
    try:
        sender = taskiq_worker.FolderBroker(worker.folder)

        async def kick() -> tuple[str, Trace]:
            with start_trace("http", "POST /reports/") as request:
                kicked = await taskiq_worker.count_users.kicker().with_broker(sender).kiq(3)
            return kicked.task_id, request

        task_id, request = asyncio.run(kick())
        worker.wait_for(lambda line: line.get("trace") == task_id)
    finally:
        worker.stop()
    lines = worker.lines()
    (run_line,) = (line for line in lines if line.get("trace") == task_id)
    name = f"taskiq@{socket.gethostname()}:{run_line['pid']}"

    # Run by the CLI's worker process, linked to the kick in this one.
    assert run_line == {
        "pid": IsPositiveInt,
        "trace": task_id,
        "worker": name,
        "state": "SUCCESS",
        "links": [request.id],
        "logs": [],
    }
    assert run_line["pid"] not in {os.getpid(), worker.process.pid}

    assert [line["event"] for line in lines if line.get("id") == task_id] == ["started", "finished"]
    # Its last word, as its broker shuts down.
    assert [line["event"] for line in lines if "event" in line and line.get("worker") == name][-1] == "worker_stopped"
