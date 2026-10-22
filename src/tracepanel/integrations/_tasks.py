"""What every task queue integration shares: the `task.*` model, independent of the queue.

A task run is a `task` trace (root span `task.run`), an enqueue a `task.enqueue` span; `task.system` says which
queue ran it (celery, django, …), so the Tasks page, search and analysis treat them alike. When the queue can carry
the enqueuing span to the worker (Celery headers), the run links to it at once; when it can't (Django Tasks), the
store links them by `task.id`.

While a task is in flight, before its trace exists, task events tell the agent where it is: `enqueued`, `started`,
`retried`, `finished`, a worker's `heartbeat`, and its `worker_stopped` as it stops cleanly. The console lists the
tasks queued, running and waiting to be retried from them (the Tasks page's Live). Any queue sends the same events;
`system` says which one. A task run eagerly, inside its caller, sends none: it is never in flight apart from it.

Every queue names the same things alike: `task.worker` (the worker that ran it), `task.eta` (when it was due to run,
ISO 8601), `task.priority`, `task.timeout` (the time limit in seconds, on a run that hit it) and the workflow it
belongs to: `task.parent_id` (the task it follows: the one whose run sent it, or the one it waits for),
`task.root_id` (the first task of the workflow) and `task.group_id` (the group it runs in).
"""

import contextlib
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from tracepanel.core.context import now_us
from tracepanel.core.frames import safe_repr
from tracepanel.core.model import Attrs, Trace
from tracepanel.storage.transport import get_writer, send_event

MAX_REPR = 2000
# Arguments in a task event are for a glance at a list: shorter than in the trace.
MAX_EVENT_REPR = 200
# How often a worker process says it is alive, with the tasks it runs (the agent drops a worker, and a running task,
# after three heartbeats without news of it).
HEARTBEAT_S = 30.0
# The caller's trace kind -> task.trigger.
TRIGGERS = {"http": "request", "task": "task", "command": "command", "websocket": "request"}


@dataclass(slots=True)
class TaskRun:
    """A task run in progress: the open trace context and the trace itself."""

    context: contextlib.AbstractContextManager[Trace]
    trace: Trace


def task_repr(name: str, value: object) -> str:
    """A value for a task trace (its result, a retry's reason): masked like locals, cut to a readable size."""
    return _cut(safe_repr(name, value), MAX_REPR)


@dataclass(frozen=True, slots=True)
class Arguments:
    """A task call's arguments as text, rendered once: the same text in each of its events and in its trace.

    Built from what each side has: the caller's values when it enqueues, the worker's once the message is decoded.
    """

    args: str
    kwargs: str

    def event(self) -> dict[str, object]:
        """The task event's `args` and `kwargs`, cut to a glance."""
        return {"args": _cut(self.args, MAX_EVENT_REPR), "kwargs": _cut(self.kwargs, MAX_EVENT_REPR)}

    def attrs(self) -> dict[str, str]:
        """The task trace's `task.args` and `task.kwargs`."""
        return {"task.args": _cut(self.args, MAX_REPR), "task.kwargs": _cut(self.kwargs, MAX_REPR)}


def arguments(args: object, kwargs: object, *, shown: tuple[object, object] = (None, None)) -> Arguments:
    """A call's arguments as text, alike on both sides of the queue.

    `shown`: the queue's own text for them, carried in the message (Celery's argsrepr and kwargsrepr), taken as it
    is, so an app that hides a secret there hides it here too. Otherwise the values go positional as a tuple and by
    name as a dict, masked like locals: a serializer hands the worker a list where the caller passed a tuple.
    """
    shown_args, shown_kwargs = shown
    return Arguments(
        args=shown_args if isinstance(shown_args, str) and shown_args else safe_repr("args", _positional(args)),
        kwargs=shown_kwargs if isinstance(shown_kwargs, str) and shown_kwargs else safe_repr("kwargs", _named(kwargs)),
    )


def _positional(args: object) -> object:
    if args is None:
        return ()
    return tuple(args) if isinstance(args, (list, tuple)) else args


def _named(kwargs: object) -> object:
    if kwargs is None:
        return dict[str, object]()
    return dict(kwargs) if isinstance(kwargs, Mapping) else kwargs


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def trigger(caller: Trace | None) -> str:
    return TRIGGERS.get(caller.kind, "other") if caller is not None else "other"


def eta(at: object) -> str | None:
    """When a task is due, as `task.eta`: ISO 8601, from a datetime (naive: UTC), epoch milliseconds or text."""
    if isinstance(at, datetime):
        return (at if at.tzinfo else at.replace(tzinfo=UTC)).isoformat()
    if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0:
        return datetime.fromtimestamp(at / 1000, tz=UTC).isoformat()
    return at if isinstance(at, str) and at else None


@dataclass(frozen=True, slots=True)
class Workflow:
    """Where a task stands among others: the task it follows, the workflow's first task, the group it runs in."""

    parent_id: str | None = None
    root_id: str | None = None
    group_id: str | None = None

    def attrs(self, task_id: str) -> Attrs:
        """The run's `task.parent_id`, `task.root_id` and `task.group_id`, those it has: a task is not its own root."""
        known = {"task.parent_id": self.parent_id, "task.root_id": self.root_id, "task.group_id": self.group_id}
        return {key: value for key, value in known.items() if value and value != task_id}


def sent_from(trace: Trace | None, task_id: str = "") -> Workflow:
    """The workflow of a task sent from inside a task's run: that run's task is its parent, the run's root its root.

    Not when the run sends its own task again (a retry): the task keeps the workflow it had.
    """
    if trace is None or trace.kind != "task":
        return Workflow()
    parent = str(trace.attrs.get("task.id") or "")
    if not parent or parent == task_id:
        return Workflow()
    root = trace.attrs.get("task.root_id")
    return Workflow(parent_id=parent, root_id=str(root) if root else parent)


def task_event(event: str, system: str, task_id: str, **fields: object) -> None:
    """A task event to the agent: what this step knows, None fields left out."""
    known = {key: value for key, value in fields.items() if value is not None}
    send_event({"event": event, "system": system, "id": task_id, "time": now_us(), **known})


def event_repr(name: str, value: object) -> str:
    """A value for a task event (a retry's reason): masked like locals, cut to a glance."""
    return _cut(safe_repr(name, value), MAX_EVENT_REPR)


class Heartbeat:
    """A worker process's heartbeat: every HEARTBEAT_S, the worker alive and the ids of the tasks this process runs.

    One thread per process, started by the first task (or the worker's start) in it: a forked pool process or work
    horse starts its own, since threads don't survive a fork. A worker that stops cleanly says so (`stopped`): the
    console drops it at once, not after three heartbeats missed.
    """

    def __init__(self, system: str, running: Callable[[], list[str]]) -> None:
        self.system = system
        self.running = running
        self.worker = ""
        self._pid = 0
        self._done = threading.Event()

    def start(self, worker: str) -> None:
        """This process's thread, started once; `worker` is the worker it speaks for, kept if empty."""
        self.worker = worker or self.worker
        if self._pid == os.getpid():
            return
        self._pid = os.getpid()
        self._done = threading.Event()
        threading.Thread(
            target=self._run, args=(self._done,), name=f"tracepanel-heartbeat-{self.system}", daemon=True
        ).start()

    def beat(self) -> None:
        """One heartbeat now: a worker that starts says so before it runs anything, for the console's workers."""
        task_event("heartbeat", self.system, "", worker=self.worker or None, ids=self.running())

    def stopped(self, worker: str = "") -> None:
        """A worker stopped cleanly: it says so, and is written before the process exits.

        This process's own worker by default, whose heartbeats end (a worker started again in it beats again); or
        `worker`, one this process speaks for (a cluster for a worker process it replaced).
        """
        name = worker or self.worker
        if name == self.worker:
            self._done.set()
            self._pid = 0
        if name:
            task_event("worker_stopped", self.system, "", worker=name)
            get_writer().flush()

    def _run(self, done: threading.Event) -> None:
        while not done.wait(HEARTBEAT_S):
            self.beat()
