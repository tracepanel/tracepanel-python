import logging
import socket
import time
from datetime import timedelta
from multiprocessing import Value
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytest.importorskip("django_q")

from dirty_equals import IsInstance, IsInt, IsList, IsPartialDict, IsPositiveInt, IsStr
from django.contrib.auth.models import User
from django.utils import timezone
from django_q import tasks as django_q_tasks
from django_q.brokers import get_broker
from django_q.cluster import Sentinel
from django_q.conf import Conf
from django_q.models import OrmQ, Schedule
from django_q.monitor import monitor
from django_q.queues import Queue
from django_q.scheduler import scheduler
from django_q.signing import SignedPackage
from django_q.tasks import Chain, async_chain, async_task, schedule
from django_q.worker import worker
from jsonschema import Draft202012Validator

from tests.agentstore import AgentStore, issues
from tests.profiles import busy, share
from tests.tasklive import Recorder, heartbeats, live, live_when, named, setenv, stopped, without_worker_events
from tracepanel.core import patching
from tracepanel.core.context import start_trace
from tracepanel.core.model import Link, Span, Trace
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import complete, set_writer

logger = logging.getLogger("tests.jobs")
NAME = "tests.django.test_django_q"


def count_users(label: str) -> str:
    logger.warning("counting for %s", label)
    return f"{label}: {User.objects.count()}"


def crunch(label: str) -> str:
    busy()
    return label


def sync_crm() -> None:
    message = "CRM is down"
    raise ConnectionError(message)


def housekeeping() -> None:
    pass


def add(a: int, b: int = 0) -> int:
    return a + b


def fan_out() -> str:
    # A task that enqueues another from inside its run.
    return str(async_task(add, 1))


def overruns() -> None:
    # Longer than its timeout: django-q's timeout handler stops it.
    time.sleep(5)


def groups_per_user() -> int:
    # N+1 inside a task: the groups of every user are loaded one by one.
    return sum(len(list(user.groups.all())) for user in User.objects.order_by("id"))


def report(task: Any) -> None:  # noqa: ANN401 (django_q.models.Task)
    logger.warning("task %s gave %s", task.id, task.result)


def broken_hook(task: Any) -> None:  # noqa: ANN401
    message = "hook failed"
    raise ValueError(message)


def work() -> None:
    """What a cluster does, in this process: pull what is due, run it in a worker, save it in the monitor.

    Again until nothing is due: the monitor enqueues a chain's next step.
    """
    broker = get_broker()
    while batch := broker.dequeue():
        task_queue, result_queue = Queue(), Queue()
        for ack_id, payload in batch:
            task = SignedPackage.loads(payload)
            # What the cluster's pusher adds.
            task["cluster"] = Conf.CLUSTER_NAME
            task["ack_id"] = ack_id
            task_queue.put(task)
        task_queue.put("STOP")
        worker(task_queue, result_queue, Value("f", -1))
        result_queue.put("STOP")
        monitor(result_queue, broker)


def tasks(traces: list[Trace]) -> list[Trace]:
    return [trace for trace in traces if trace.kind == "task"]


def runs_by_id(traces: list[Trace]) -> dict[str, Trace]:
    return {str(trace.attrs["task.id"]): trace for trace in tasks(traces) if "task.id" in trace.attrs}


def enqueues(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "task.enqueue"]


def workflow(trace: Trace) -> dict[str, object]:
    keys = ("task.parent_id", "task.root_id", "task.group_id")
    return {key: trace.attrs[key] for key in keys if key in trace.attrs}


@pytest.mark.django_db(transaction=True)
def test_enqueue_and_run_are_linked(traces: list[Trace], users: list[User], validator: Draft202012Validator) -> None:
    with start_trace("http", "POST /reports/") as request:
        task_id = async_task(count_users, "queued")
    work()
    (run,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert (run.name, enqueue.name) == (f"{NAME}.count_users", f"enqueue {NAME}.count_users")
    assert run.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert enqueue.attrs == {
        "task.system": "django-q",
        "task.name": f"{NAME}.count_users",
        "code.origin": IsStr(regex=r"tests/django/test_django_q\.py:\d+ in test_enqueue_and_run_are_linked"),
        "task.id": task_id,
        "task.queue": "default",
        "task.eager": False,
        "messaging.system": "database",
    }
    assert {key: value for key, value in run.attrs.items() if key.startswith("task.")} == {
        "task.system": "django-q",
        "task.name": f"{NAME}.count_users",
        "task.id": task_id,
        "task.queue": "default",
        "task.worker": IsStr(regex=r"django-q@.+:\d+"),
        "task.retries": 0,
        "task.eager": False,
        "task.trigger": "request",
        "task.queue_wait": IsInt(ge=0),
        "task.args": "('queued',)",
        "task.kwargs": "{}",
        "task.state": "SUCCESS",
        "task.result": "'queued: 5'",
    }
    assert run.attrs["messaging.system"] == "database"

    assert [log.message for log in run.logs] == ["counting for queued"]
    assert [span.name for span in run.spans if span.kind == "sql"] == ["SELECT COUNT auth_user"]
    validator.validate(run.to_dict())


@pytest.mark.django_db(transaction=True)
def test_a_failed_task(traces: list[Trace]) -> None:
    async_task(sync_crm)
    work()
    (run,) = tasks(traces)

    assert (run.status, run.attrs["task.state"]) == ("error", "FAILURE")
    assert [(error.type, error.message, error.handled) for error in run.errors] == [
        ("builtins.ConnectionError", "CRM is down", False)
    ]
    assert run.errors[0].frames[-1].locals == {"message": "'CRM is down'"}
    assert "task.result" not in run.attrs


@pytest.mark.django_db(transaction=True)
def test_a_failed_task_delivered_again_counts_the_runs_before(traces: list[Trace]) -> None:
    task_id = async_task(sync_crm)
    work()
    # The broker delivers it again once its lock expires (the `retry` setting).
    OrmQ.objects.update(lock=timezone.now() - timedelta(seconds=1))
    work()

    assert [(run.attrs["task.id"], run.attrs["task.retries"]) for run in tasks(traces)] == [(task_id, 0), (task_id, 1)]


@pytest.mark.django_db
def test_sync_runs_inside_the_enqueue(traces: list[Trace], users: list[User]) -> None:
    with start_trace("http", "POST /now/") as request:
        task_id = async_task(count_users, "inline", sync=True)
    (run,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert enqueue.attrs == {
        "task.system": "django-q",
        "task.name": f"{NAME}.count_users",
        "code.origin": IsStr,
        "task.id": task_id,
        "task.queue": "default",
        "task.eager": True,
    }
    assert {key: value for key, value in run.attrs.items() if key.startswith(("task.", "service.role"))} == {
        "task.system": "django-q",
        "task.name": f"{NAME}.count_users",
        "task.id": task_id,
        "task.queue": "default",
        "task.retries": 0,
        "task.eager": True,
        "task.trigger": "request",
        "service.role": "web",
        "task.args": "('inline',)",
        "task.kwargs": "{}",
        "task.state": "SUCCESS",
        "task.result": "'inline: 5'",
    }
    assert run.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]


@pytest.mark.django_db
def test_a_sync_task_that_fails_raises_in_the_caller(traces: list[Trace]) -> None:
    with start_trace("http", "POST /now/"), pytest.raises(ConnectionError):
        async_task(sync_crm, sync=True)
    (run,) = tasks(traces)

    assert (run.status, run.attrs["task.state"]) == ("error", "FAILURE")
    assert [(error.type, error.handled) for error in run.errors] == [("builtins.ConnectionError", False)]


@pytest.mark.django_db(transaction=True)
def test_an_async_task_bound_before_tracepanel_still_links(traces: list[Trace]) -> None:
    # A module that took async_task before tracepanel wrapped it: the enqueue span is recorded at pre_enqueue.
    unwrapped = patching.original(django_q_tasks, "async_task")
    with start_trace("http", "POST /reports/") as request:
        task_id = unwrapped(add, 2, 3)
    work()
    (run,) = tasks(traces)
    (enqueue,) = enqueues(request)

    assert enqueue.attrs == IsPartialDict({"task.name": f"{NAME}.add", "task.id": task_id, "task.eager": False})
    assert run.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert run.attrs["task.result"] == "5"


@pytest.mark.django_db(transaction=True)
def test_ignored_tasks(traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")

    async_task(housekeeping)
    work()

    assert tasks(traces) == []


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


@pytest.mark.django_db(transaction=True)
def test_a_queued_task_tells_the_agent_each_step(recorder: Recorder) -> None:
    with start_trace("http", "POST /reports/") as request:
        task_id = async_task(add, 3, b=4)
    work()
    (enqueue,) = enqueues(request)
    (run,) = tasks(recorder.traces)
    name = str(run.attrs["task.worker"])

    assert without_worker_events(recorder.events) == [
        {
            "event": "enqueued",
            "system": "django-q",
            "id": task_id,
            "time": IsPositiveInt,
            "name": f"{NAME}.add",
            "queue": "default",
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{'b': 4}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "django-q",
            "id": task_id,
            "time": IsPositiveInt,
            "name": f"{NAME}.add",
            "queue": "default",
            "worker": name,
            "retries": 0,
            "args": "(3,)",
            "kwargs": "{'b': 4}",
        },
        {
            "event": "finished",
            "system": "django-q",
            "id": task_id,
            "time": IsPositiveInt,
            "worker": name,
            "state": "SUCCESS",
        },
    ]

    # The worker process says it is there as it spawns, before it runs anything.
    assert heartbeats(recorder.events, name)[0] == {
        "event": "heartbeat",
        "system": "django-q",
        "id": "",
        "time": IsPositiveInt,
        "worker": name,
        "ids": [],
    }

    # The task's trace shows its arguments as its events do.
    assert run.attrs == IsPartialDict({"task.args": "(3,)", "task.kwargs": "{'b': 4}"})


@pytest.mark.django_db(transaction=True)
def test_sync_and_ignored_tasks_send_no_events(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")
    # django-q runs a worker inside async_task(): no worker process says it is there.
    async_task(add, 1, sync=True)
    async_task(housekeeping)
    work()

    assert without_worker_events(recorder.events) == []
    assert [event["worker"] for event in recorder.events] == [IsStr(regex=r"django-q@.+:\d+")]


@pytest.mark.django_db(transaction=True)
def test_arguments_are_left_out_of_the_events_too(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    async_task(add, 7)
    work()

    assert [{"args", "kwargs"} & event.keys() for event in without_worker_events(recorder.events)] == [set()] * 3


def test_django_q_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


def test_the_cluster_says_a_worker_it_replaced_stopped(recorder: Recorder) -> None:
    # A worker process recycled after its tasks (timer -2), and the monitor, both replaced by the sentinel.
    recycled = SimpleNamespace(pid=4242, name="Process-w", timer=SimpleNamespace(value=-2))
    monitor_process = SimpleNamespace(pid=4243, name="Process-m")
    spawned: list[str] = []
    sentinel = cast(
        "Sentinel",
        SimpleNamespace(
            pool=[recycled],
            monitor=monitor_process,
            pusher=None,
            reincarnations=0,
            spawn_worker=lambda: spawned.append("worker"),
            spawn_monitor=lambda: spawned.append("monitor"),
        ),
    )

    Sentinel.reincarnate(sentinel, monitor_process)
    Sentinel.reincarnate(sentinel, recycled)
    worker_name = f"django-q@{socket.gethostname()}:4242"

    assert spawned == ["monitor", "worker"]

    # The monitor runs no tasks: only the worker is gone from the console.
    assert stopped(recorder.events, worker_name) == [
        {"event": "worker_stopped", "system": "django-q", "id": "", "time": IsPositiveInt, "worker": worker_name}
    ]
    assert recorder.events == stopped(recorder.events, worker_name)


# The store a running task reads the agent's list from, and what it read: work() runs it in the test's thread.
watched: list[AgentStore] = []
seen: list[dict[str, Any]] = []


def look() -> None:
    (store,) = watched
    seen.append(live_when(store, lambda answer: bool(answer["tasks"]) and answer["tasks"][0]["state"] == "running"))


@pytest.mark.django_db(transaction=True)
def test_the_agent_lists_a_task_queued_then_running_until_it_ends(store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    watched[:] = [store]
    seen.clear()
    try:
        task_id = async_task(look)
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued == {"now": IsPositiveInt, "tasks": [IsPartialDict(id=task_id)], "workers": IsInstance(list)}
        assert queued["tasks"][0] == {
            "id": task_id,
            "system": "django-q",
            "name": f"{NAME}.look",
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

        work()
        (running,) = seen
        name = running["tasks"][0]["worker"]

        assert running["tasks"] == [
            IsPartialDict(id=task_id, state="running", worker=IsStr(regex=r"django-q@.+:\d+"), started_at=IsInt)
        ]
        assert named(running, name) == [
            {"name": name, "system": "django-q", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        done = live_when(store, lambda answer: not answer["tasks"])

        assert named(done, name) == [IsPartialDict(name=name, in_flight=0)]
    finally:
        set_writer(None)


@pytest.mark.django_db(transaction=True)
def test_a_task_stopped_by_its_timeout_says_so(recorder: Recorder) -> None:
    task_id = async_task(overruns, timeout=1)
    work()
    (run,) = tasks(recorder.traces)

    assert run.attrs == IsPartialDict({"task.state": "FAILURE", "task.timeout": 1})
    assert [(error.type, error.handled) for error in run.errors] == [("django_q.exceptions.TimeoutException", False)]

    assert without_worker_events(recorder.events)[-1] == {
        "event": "finished",
        "system": "django-q",
        "id": task_id,
        "time": IsPositiveInt,
        "worker": run.attrs["task.worker"],
        "state": "FAILURE",
    }


@pytest.mark.django_db(transaction=True)
def test_a_chain_s_steps_follow_each_other(traces: list[Trace]) -> None:
    chain = Chain()
    chain.append(add, 1)
    chain.append(add, 2)
    chain.append(add, 3)
    with start_trace("http", "POST /reports/") as request:
        group = chain.run()
    work()
    steps = sorted(tasks(traces), key=lambda trace: trace.start)
    first, second, _ = (str(step.attrs["task.id"]) for step in steps)

    # The group django-q gives a chain's steps is the chain: they follow each other, they are not a group.
    assert group
    assert [workflow(step) for step in steps] == [
        {},
        {"task.parent_id": first, "task.root_id": first},
        {"task.parent_id": second, "task.root_id": first},
    ]

    # The first was enqueued by the request, each next one by the monitor once the step before was done.
    (enqueue,) = enqueues(request)
    assert [step.links for step in steps] == [
        [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)],
        [Link(kind="enqueued_by", trace_id=steps[0].id, span_id=steps[0].spans[0].id)],
        [Link(kind="enqueued_by", trace_id=steps[1].id, span_id=steps[1].spans[0].id)],
    ]

    assert [step.attrs["task.trigger"] for step in steps] == ["request", "task", "task"]


@pytest.mark.django_db(transaction=True)
def test_async_chain_s_steps_follow_each_other_and_are_no_group(traces: list[Trace]) -> None:
    async_chain([(add, (1,)), (add, (2,))])
    work()
    first, second = sorted(tasks(traces), key=lambda trace: trace.start)
    first_id = str(first.attrs["task.id"])

    assert [workflow(first), workflow(second)] == [{}, {"task.parent_id": first_id, "task.root_id": first_id}]

    assert second.links == [Link(kind="enqueued_by", trace_id=first.id, span_id=first.spans[0].id)]


@pytest.mark.django_db(transaction=True)
def test_a_task_enqueued_from_a_task_follows_it(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports/"):
        outer = async_task(fan_out)
    work()
    runs = runs_by_id(traces)
    (inner,) = set(runs) - {outer}
    (sent,) = enqueues(runs[outer])

    assert workflow(runs[inner]) == {"task.parent_id": outer, "task.root_id": outer}
    assert runs[inner].links == [Link(kind="enqueued_by", trace_id=runs[outer].id, span_id=sent.id)]
    assert runs[inner].attrs["task.trigger"] == "task"


@pytest.mark.django_db(transaction=True)
def test_a_group_s_tasks_share_its_id(traces: list[Trace]) -> None:
    ids = [async_task(add, 5, group="reports"), async_task(add, 6, group="reports")]
    work()
    runs = runs_by_id(traces)

    assert [workflow(runs[task_id]) for task_id in ids] == [{"task.group_id": "reports"}] * 2


@pytest.mark.django_db(transaction=True)
def test_hooks_run_as_traces_linked_to_their_task(traces: list[Trace]) -> None:
    done = async_task(add, 7, hook=report)
    failed = async_task(add, 8, hook=f"{NAME}.broken_hook")
    work()
    runs = runs_by_id(traces)
    hooks = {str(trace.attrs["task.parent_id"]): trace for trace in tasks(traces) if "task.callback" in trace.attrs}

    assert {key: value for key, value in hooks[done].attrs.items() if key.startswith("task.")} == {
        "task.system": "django-q",
        "task.name": f"{NAME}.report",
        "task.queue": "default",
        "task.worker": runs[done].attrs["task.worker"],
        "task.eager": False,
        "task.trigger": "task",
        "task.callback": "hook",
        "task.parent_id": done,
        "task.root_id": done,
        "task.state": "SUCCESS",
    }
    assert hooks[done].links == [Link(kind="enqueued_by", trace_id=runs[done].id, span_id=runs[done].spans[0].id)]
    assert [log.message for log in hooks[done].logs] == [f"task {done} gave 7"]

    assert hooks[failed].attrs == IsPartialDict(
        {"task.name": f"{NAME}.broken_hook", "task.parent_id": failed, "task.state": "FAILURE"}
    )
    assert [(error.type, error.message, error.handled) for error in hooks[failed].errors] == [
        ("builtins.ValueError", "hook failed", False)
    ]


@pytest.mark.django_db(transaction=True)
def test_a_schedule_says_when_it_is_due_and_its_runs_are_scheduled(traces: list[Trace]) -> None:
    due = timezone.now() - timedelta(seconds=1)
    with start_trace("http", "POST /reports/") as request:
        created = schedule(f"{NAME}.add", 2, 3, name="nightly", next_run=due)
    (enqueue,) = enqueues(request)
    # What the cluster's scheduler does every few seconds: enqueue what is due.
    scheduler(get_broker())
    work()
    (run,) = tasks(traces)

    assert enqueue.attrs == {
        "task.system": "django-q",
        "task.name": f"{NAME}.add",
        "code.origin": IsStr,
        "task.queue": "default",
        "task.eager": False,
        "task.eta": due.isoformat(),
        "messaging.system": "database",
    }
    assert run.attrs == IsPartialDict(
        {"task.trigger": "schedule", "task.group_id": "nightly", "task.state": "SUCCESS", "task.result": "5"}
    )
    assert not Schedule.objects.filter(pk=created.pk).exists()


@pytest.mark.django_db
def test_issues_inside_a_task(traces: list[Trace], users: list[User]) -> None:
    async_task(groups_per_user, sync=True)
    (run,) = tasks(traces)

    assert [issue.kind for issue in issues(run)] == ["n+1"]


@pytest.mark.django_db(transaction=True)
def test_arguments_can_be_left_out(traces: list[Trace], monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")

    async_task(add, 7)
    work()
    (run,) = tasks(traces)

    assert {"task.args", "task.kwargs", "task.result"} & run.attrs.keys() == set()


@pytest.mark.django_db(transaction=True)
def test_tasks_listing_and_links(store: AgentStore) -> None:
    writer = store.writer()
    set_writer(writer)
    try:
        with start_trace("http", "POST /reports/") as request:
            async_task(add, 3)
            async_task(sync_crm)
        # A request's trace is written as it ends.
        complete(request)
        work()
        writer.flush()
    finally:
        set_writer(None)
    (runs, _) = store.search_traces(f"task:{NAME}.add")

    assert store.list_tasks() == IsList(
        IsPartialDict(task=f"{NAME}.add", system="django-q", runs=1, failures=0),
        IsPartialDict(task=f"{NAME}.sync_crm", system="django-q", runs=1, failures=1),
        check_order=False,
    )
    assert store.related(str(runs[0]["id"]))["enqueued_by"] == IsPartialDict(id=request.id)


def profiled(traces: list[Trace]) -> dict[str, bool]:
    """Each run, by its arguments: whether it has a profile."""
    return {str(trace.attrs["task.args"]): trace.profile is not None for trace in tasks(traces)}


@pytest.mark.django_db(transaction=True)
def test_a_task_a_profiled_request_enqueues_is_profiled_too(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports", profile=True):
        async_task(crunch, "asked")
    with start_trace("http", "GET /reports"):
        async_task(crunch, "plain")
    work()

    assert profiled(traces) == {"('asked',)": True, "('plain',)": False}


@pytest.mark.django_db(transaction=True)
def test_a_sync_task_is_in_its_profiled_callers_profile(traces: list[Trace]) -> None:
    with start_trace("http", "POST /reports", profile=True) as request:
        async_task(crunch, "sync", sync=True)

    assert profiled(traces) == {"('sync',)": False}
    assert share(request, "busy") > 0
