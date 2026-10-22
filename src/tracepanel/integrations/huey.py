"""Huey: every task a consumer runs is a `task` trace, every enqueue a `task.enqueue` span, and the two are linked.

Turned on by itself when Huey is installed, for every Huey instance (Django's `huey.contrib.djhuey` one too):
`Huey.enqueue` records the enqueue span, `Huey.execute` holds the run's trace open until Huey is done with the task
(the next step of a pipeline is sent from inside it), and Huey's own hooks say what happens in between: the
`executing` signal starts the trace, a `post_execute` hook gives the outcome, the `retrying` signal tells of a retry.
A Huey message has nowhere to carry the enqueuing trace: tracepanel keeps it in the instance's storage next to the
task (`tracepanel:<task id>`, read and deleted as the task starts), so the run links back to its enqueue span
(`enqueued_by`) and knows how long it waited in the queue.

Immediate mode (`immediate=True`) runs a task inside `enqueue()`: like an eager Celery task, its trace links to the
enqueue span in the caller's trace, and it is never in flight apart from it.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine.

A task's trace reaches the agent when it ends. Until then task events say where it is: `enqueued` after the enqueue
(with the enqueuing trace and span), `started` as it runs, `retried` (with the error) when Huey puts it back, then
`finished`, also for a task revoked or expired before it ran. A delayed task moved to the queue when due is not
enqueued again. Each worker says it is there as it starts and every process that runs tasks sends a `heartbeat` with
them, so a task whose worker was killed leaves the console's Live list. A worker that stops says so
(`worker_stopped`): a process worker as it shuts down, thread and greenlet workers (their process) as the consumer
exits. Immediate mode sends none.

A task's trace says the worker process that ran it (`task.worker`), when a delayed task or retry was due
(`task.eta`), its priority and, on a run stopped by its time limit (TaskTimeout), the limit (`task.timeout`). A task
sent from inside another's run (the next step of a pipeline, a chord's callback, an `on_error` task, any enqueue in
a task) records that run as `task.parent_id`, with the first task of the line as `task.root_id`; the tasks of a group
or a chord share a `task.group_id`. Periodic tasks run with the trigger `schedule`.
"""

import contextlib
import functools
import multiprocessing
import os
import socket
import sys
import weakref
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from tracepanel.core import conf, errors, forks, registry, services
from tracepanel.core.context import adopt, current_span, current_trace, now_us, record_span, recorded, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link, Trace
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import (
    Heartbeat,
    TaskRun,
    Workflow,
    arguments,
    eta,
    event_repr,
    sent_from,
    task_event,
    task_repr,
    trigger,
)
from tracepanel.storage import agent
from tracepanel.storage.transport import complete, get_writer, role

SYSTEM = "huey"
# Where tracepanel keeps what a task's run needs to know of its enqueue, in the Huey instance's storage.
META = "tracepanel:{}"
# The post_execute hook tracepanel adds to each Huey instance.
HOOK = "tracepanel"
# The task djhuey's Django Tasks backend runs each Django task in: the Django Tasks integration traces those runs.
DJANGO_TASKS = "huey.contrib.djhuey.tasks_backend.djtasks.run"
# Storage classes (and their subclasses) -> messaging.system; Redis says its server too.
STORAGES = {
    "RedisStorage": "redis",
    "MemoryStorage": "memory",
    "SqliteStorage": "sqlite",
    "PostgresStorage": "postgresql",
    "FileStorage": "filesystem",
}


@dataclass(slots=True)
class _Run:
    """A task run in progress: its trace, what its enqueue said, and what Huey said of its outcome."""

    run: TaskRun
    task_id: str
    eager: bool
    meta: dict[str, Any]
    state: str | None = None
    error: BaseException | None = field(default=None)


# The run open in this thread: Huey's signals and hooks for a task come from the thread that runs it.
_run: ContextVar[_Run | None] = ContextVar("tracepanel_huey_run", default=None)
# (task id, its enqueue's meta) while immediate mode runs that task inside enqueue().
_inline: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar("tracepanel_huey_inline", default=None)
# The group or chord whose tasks this thread enqueues now.
_group: ContextVar[str | None] = ContextVar("tracepanel_huey_group", default=None)
# The tasks this process runs now, for its heartbeat; one that was retried leaves at once.
_running: set[str] = set()
_lock = forks.lock()
# The Huey instances that have tracepanel's signal receiver and hook.
_connected: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _running_ids() -> list[str]:
    with _lock:
        return sorted(_running)


_heart = Heartbeat(SYSTEM, _running_ids)


def _worker() -> str:
    """This worker process, as its events and traces name it (thread workers share their process's name)."""
    return f"huey@{socket.gethostname()}:{os.getpid()}"


def _name(task: Any) -> str:  # noqa: ANN401 (huey.api.Task)
    """`app.tasks.send_digest`: the task's module and name, as Huey's registry knows it."""
    kind = type(task)
    return f"{kind.__module__}.{kind.__name__}"


def _skipped(name: str) -> bool:
    config = conf.get_config()
    return conf.matches(name, config.ignore_tasks) or (name == DJANGO_TASKS and config.django_tasks_enabled)


def _broker(huey: Any) -> Attrs:  # noqa: ANN401 (huey.Huey)
    """Where the tasks wait: the Redis server, or the storage Huey keeps them in."""
    storage = getattr(huey, "storage", None)
    pool = getattr(getattr(storage, "conn", None), "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None)
    if isinstance(kwargs, dict) and kwargs.get("host"):
        return services.for_broker(f"redis://{kwargs['host']}:{kwargs.get('port', 6379)}")
    system = next((STORAGES[kind.__name__] for kind in type(storage).__mro__ if kind.__name__ in STORAGES), None)
    return {"messaging.system": system} if system else {}


def _due(huey: Any, task: Any) -> str | None:  # noqa: ANN401
    """When the task is due, as `task.eta`: Huey keeps naive datetimes, in UTC unless the instance says local time."""
    at = getattr(task, "eta", None)
    if at is None:
        return None
    return eta(at if getattr(huey, "utc", True) or at.tzinfo else at.astimezone())


def _scheduling(huey: Any, task: Any) -> Attrs:  # noqa: ANN401
    attrs: Attrs = {}
    due = _due(huey, task)
    if due:
        attrs["task.eta"] = due
    if isinstance(task.priority, int):
        attrs["task.priority"] = task.priority
    return attrs


def _arguments(task: Any) -> dict[str, object]:  # noqa: ANN401
    if not conf.get_config().capture_task_args:
        return {}
    return arguments(task.args, task.kwargs).event()


# ---------- the enqueuing side ----------


def _meta(task: Any, trace: Trace | None, span_id: str) -> dict[str, Any]:  # noqa: ANN401
    """What the run needs to know of its enqueue: how it was triggered, the enqueue span, the run that sent it."""
    from huey.api import PeriodicTask  # noqa: PLC0415 (optional dependency)

    meta: dict[str, Any] = {
        "trigger": "schedule" if isinstance(task, PeriodicTask) else trigger(trace),
        "published": now_us(),
        "retries": 0,
    }
    if trace is not None:
        meta["parent"] = f"{trace.id}:{span_id}"
    workflow = sent_from(trace, task.id)
    if workflow.parent_id:
        meta["parent_task"] = workflow.parent_id
        meta["root_task"] = workflow.root_id
    group = _chord(task) or _group.get()
    if group:
        meta["group"] = group
    return meta


def _chord(task: Any) -> str | None:  # noqa: ANN401
    """The chord a task belongs to: Huey marks the last step of each member's pipeline with the chord's id."""
    while task is not None:
        chord = getattr(task, "chord_config", None)
        if chord is not None:
            return str(chord.cid)
        task = task.on_complete
    return None


def _keep(huey: Any, task_id: str, meta: dict[str, Any], *, first: bool) -> bool:  # noqa: ANN401
    """Keep the meta next to the task; on its first enqueue only if there is none (a delayed task moved has one)."""
    try:
        if first:
            return bool(huey.put_if_empty(META.format(task_id), meta))
        huey.put(META.format(task_id), meta)
    except Exception:  # noqa: BLE001 (the app's enqueue goes on without it)
        return False
    return True


def _take(huey: Any, task_id: str) -> dict[str, Any]:  # noqa: ANN401
    """The meta its enqueue kept, removed: the next attempt gets its own."""
    key = META.format(task_id)
    try:
        # A peek, then a delete: an expiring storage's pop leaves the key.
        meta = huey.get(key, peek=True)
        if meta is not None:
            huey.delete(key)
    except Exception:  # noqa: BLE001 (a task enqueued without tracepanel, or the storage failed: no link)
        return {}
    return meta if isinstance(meta, dict) else {}


def _enqueue(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def enqueue(self: Any, task: Any) -> Any:  # noqa: ANN401 (huey.Huey, a task, group or chord)
        from huey.api import Task  # noqa: PLC0415 (optional dependency)

        if not isinstance(task, Task):
            # A group or a chord: the tasks it enqueues now are its members.
            token = _group.set(new_id())
            try:
                return original(self, task)
            finally:
                _group.reset(token)
        name = _name(task)
        run = _run.get()
        if _skipped(name) or (run is not None and run.task_id == task.id):
            # Huey puts a retried task back from inside its run: the `retrying` signal told of it.
            return original(self, task)
        trace = current_trace()
        start, mark, span_id = now_us(), recorded(), new_id()
        meta = _meta(task, trace, span_id)
        immediate = bool(self.immediate)
        if immediate:
            token = _inline.set((task.id, meta))
            try:
                result = original(self, task)
            finally:
                _inline.reset(token)
        else:
            first = _keep(self, task.id, meta, first=True)
            result = original(self, task)
            if not first:
                # A delayed task (or a retry) the scheduler moves to the queue: it was enqueued before.
                return result
            _enqueued(self, task, name, (trace.id, span_id) if trace is not None else None)
        if trace is not None:
            _record(self, task, name, span_id, start=start, mark=mark, eager=immediate)
        return result

    return enqueue


def _enqueued(huey: Any, task: Any, name: str, parent: tuple[str, str] | None) -> None:  # noqa: ANN401
    task_event(
        "enqueued",
        SYSTEM,
        task.id,
        name=name,
        queue=huey.name,
        retries=0,
        **_arguments(task),
        trace_id=parent[0] if parent else None,
        span_id=parent[1] if parent else None,
    )


def _record(  # noqa: PLR0913 (what the enqueue knew)
    huey: Any,  # noqa: ANN401
    task: Any,  # noqa: ANN401
    name: str,
    span_id: str,
    *,
    start: int,
    mark: int,
    eager: bool,
) -> None:
    # The storage's commands (Redis LPUSH…) go under the enqueue span; an immediate run is a trace of its own.
    adopt(span_id, mark)
    record_span(
        "task.enqueue",
        f"enqueue {name}",
        start=start,
        duration=now_us() - start,
        attrs={
            "task.system": SYSTEM,
            "task.name": name,
            "task.id": task.id,
            "task.queue": huey.name,
            "task.eager": eager,
            **_scheduling(huey, task),
            "code.origin": origin(),
            **_broker(huey),
        },
        span_id=span_id,
    )


# ---------- the worker side ----------


def _connect(huey: Any) -> None:  # noqa: ANN401
    """Tracepanel's signal receiver and post_execute hook on this instance, once, before it runs anything."""
    if huey in _connected:
        return
    from huey import signals  # noqa: PLC0415 (optional dependency)

    with _lock:
        if huey in _connected:
            return
        receive = functools.partial(_on_signal, huey)
        huey.signal(
            signals.SIGNAL_EXECUTING,
            signals.SIGNAL_RETRYING,
            signals.SIGNAL_REVOKED,
            signals.SIGNAL_EXPIRED,
            signals.SIGNAL_CANCELED,
            signals.SIGNAL_INTERRUPTED,
        )(receive)
        huey.post_execute(HOOK)(_after)
        _connected.add(huey)


def _execute(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def execute(self: Any, task: Any, timestamp: Any = None) -> Any:  # noqa: ANN401 (huey.Huey, a task, a datetime)
        _connect(self)
        # The run of a task enqueued from this one in immediate mode is its own.
        token = _run.set(None)
        try:
            return original(self, task, timestamp)
        except BaseException as exc:
            run = _run.get()
            if run is not None and run.state is None:
                _outcome(run, task, exc)
            raise
        finally:
            run = _run.get()
            _run.reset(token)
            if run is not None:
                _close(run)

    return execute


def _on_signal(huey: Any, signal: str, task: Any, *args: object) -> None:  # noqa: ANN401
    del args
    from huey import signals  # noqa: PLC0415 (optional dependency)

    if signal == signals.SIGNAL_EXECUTING:
        _start(huey, task)
    elif signal == signals.SIGNAL_RETRYING:
        _retrying(huey, task)
    elif signal in {signals.SIGNAL_REVOKED, signals.SIGNAL_EXPIRED}:
        _dropped(huey, task)
    else:
        # Canceled by a pre_execute hook, or interrupted by the worker's shutdown: unless the run's outcome says else
        # (a task that raised CancelExecution itself has one).
        run = _run.get()
        if run is not None and run.task_id == task.id:
            run.state = "REVOKED"


def _start(huey: Any, task: Any) -> None:  # noqa: ANN401
    name = _name(task)
    if _skipped(name):
        return
    started = now_us()
    eager = bool(huey.immediate)
    inline = _inline.get()
    if eager:
        meta: dict[str, Any] = inline[1] if inline is not None and inline[0] == task.id else {}
    else:
        meta = _take(huey, task.id)
        _heart.start(_worker())
    caller, caller_span = current_trace(), current_span()
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": name,
        "task.id": task.id,
        "task.queue": huey.name,
        "task.worker": None if eager else _heart.worker,
        "task.retries": int(meta.get("retries") or 0),
        "task.eager": eager,
        "task.trigger": str(meta.get("trigger") or ("request" if eager and caller is not None else "other")),
        **_broker(huey),
        **_scheduling(huey, task),
    }
    attrs.update(_workflow(meta).attrs(task.id))
    published = meta.get("published")
    if isinstance(published, int) and not eager:
        attrs["task.queue_wait"] = max(started - published, 0)
    if eager and caller is not None:
        # Runs inline, in the caller's process: web or command, not a worker.
        attrs["service.role"] = role(caller)
    if conf.get_config().capture_task_args:
        attrs.update(arguments(task.args, task.kwargs).attrs())
    link = _link(meta, caller, caller_span, eager=eager)
    context = start_trace("task", name, attrs, root_kind="task.run")
    trace = context.__enter__()
    if link is not None:
        trace.links.append(link)
    _run.set(_Run(run=TaskRun(context=context, trace=trace), task_id=task.id, eager=eager, meta=meta))
    if not eager:
        _started(huey, task, name, attrs)


def _workflow(meta: dict[str, Any]) -> Workflow:
    parent, root, group = meta.get("parent_task"), meta.get("root_task"), meta.get("group")
    return Workflow(
        parent_id=str(parent) if parent else None,
        root_id=str(root) if root else None,
        group_id=str(group) if group else None,
    )


def _link(meta: dict[str, Any], caller: Trace | None, span: Any, *, eager: bool) -> Link | None:  # noqa: ANN401
    """The enqueue span: from the meta, or the caller's span for a task run inline without one."""
    parent = meta.get("parent")
    if isinstance(parent, str) and ":" in parent:
        parent_trace, parent_span = parent.split(":", 1)
        return Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span)
    if eager and caller is not None and span is not None:
        return Link(kind="enqueued_by", trace_id=caller.id, span_id=span.id)
    return None


def _started(huey: Any, task: Any, name: str, attrs: Attrs) -> None:  # noqa: ANN401
    with _lock:
        _running.add(task.id)
    task_event(
        "started",
        SYSTEM,
        task.id,
        name=name,
        queue=huey.name,
        worker=_heart.worker,
        retries=attrs["task.retries"],
        **_arguments(task),
    )


def _after(task: Any, value: object, exception: BaseException | None) -> None:  # noqa: ANN401
    """Huey's post_execute hook: the run's outcome, once Huey has decided whether the task will be retried."""
    run = _run.get()
    if run is None or run.task_id != task.id:
        return
    trace = run.run.trace
    if exception is None:
        run.state = "SUCCESS"
        if conf.get_config().capture_task_args:
            trace.attrs["task.result"] = task_repr("result", value)
        return
    _outcome(run, task, exception)


def _outcome(run: _Run, task: Any, exception: BaseException) -> None:  # noqa: ANN401
    """A run that raised: retried while Huey has retries left for it, canceled by CancelExecution, else failed."""
    trace = run.run.trace
    kind = type(exception).__name__
    retried = bool(getattr(task, "retries", 0)) and isinstance(exception, Exception)
    run.error = exception
    if retried:
        run.state = "RETRY"
        trace.attrs["task.retry_reason"] = task_repr("reason", exception)
    else:
        run.state = "REVOKED" if kind == "CancelExecution" else "FAILURE"
    if kind == "TaskTimeout" and getattr(task, "timeout", None):
        trace.attrs["task.timeout"] = task.timeout
    if kind not in {"RetryTask", "CancelExecution"}:
        # RetryTask and CancelExecution are how a task asks for another run or none: not errors.
        errors.capture(exception, handled=retried)
    trace.status = "error" if run.state == "FAILURE" else "ok"


def _retrying(huey: Any, task: Any) -> None:  # noqa: ANN401
    """Huey puts the task back now: its next attempt counts this one, and may start in another thread at once."""
    run = _run.get()
    if run is None or run.task_id != task.id:
        return
    retries = int(run.meta.get("retries") or 0) + 1
    run.meta.update(retries=retries, published=now_us())
    if run.eager:
        return
    _keep(huey, task.id, run.meta, first=False)
    with _lock:
        _running.discard(task.id)
    error = event_repr("reason", run.error) if run.error is not None else None
    task_event("retried", SYSTEM, task.id, worker=_heart.worker or None, retries=retries, error=error)


def _dropped(huey: Any, task: Any) -> None:  # noqa: ANN401
    """A task revoked or expired before it ran: it never will, its meta goes."""
    if huey.immediate or _skipped(_name(task)):
        return
    with contextlib.suppress(Exception):
        huey.delete(META.format(task.id))
    task_event("finished", SYSTEM, task.id, worker=_worker(), state="REVOKED")


def _close(run: _Run) -> None:
    trace = run.run.trace
    state = run.state or "SUCCESS"
    trace.attrs["task.state"] = state
    run.run.context.__exit__(None, None, None)
    complete(trace)
    if run.eager or state == "RETRY":
        # A retry waits for its next attempt: it is still in flight.
        return
    with _lock:
        running = run.task_id in _running
        _running.discard(run.task_id)
    if running:
        task_event("finished", SYSTEM, run.task_id, worker=_heart.worker or None, state=state)


# ---------- the consumer ----------


def _initialize(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def initialize(self: Any) -> Any:  # noqa: ANN401 (huey.consumer.Worker)
        result = original(self)
        # The worker is there before it runs anything: the console lists it among the workers.
        _heart.start(_worker())
        _heart.beat()
        return result

    return initialize


def _shutdown(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def shutdown(self: Any) -> Any:  # noqa: ANN401 (huey.consumer.Worker)
        try:
            return original(self)
        finally:
            # A process worker ends without running anything at exit: write what is queued now, and say it stopped
            # (the process is the worker). Threads share theirs: the consumer says so as it exits.
            get_writer().flush()
            if multiprocessing.parent_process() is not None:
                _heart.stopped()

    return shutdown


def _consumer_run(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def run(self: Any) -> Any:  # noqa: ANN401 (huey.consumer.Consumer)
        try:
            return original(self)
        finally:
            # Its thread or greenlet workers stop with it, done or interrupted: the console drops them at once.
            if self.worker_type != "process":
                _heart.stopped(_worker())

    return run


def install() -> None:
    """Wrap Huey's enqueue and execute and its consumer's workers; a no-op without Huey."""
    try:
        from huey.api import Huey  # noqa: PLC0415 (optional dependency)
        from huey.consumer import Consumer, Worker  # noqa: PLC0415
    except ImportError:
        return
    if patched(Huey, "enqueue"):
        return
    patch(Huey, "enqueue", _enqueue(Huey.enqueue))
    patch(Huey, "execute", _execute(Huey.execute))
    patch(Worker, "initialize", _initialize(Worker.initialize))
    patch(Worker, "shutdown", _shutdown(Worker.shutdown))
    patch(Consumer, "run", _consumer_run(Consumer.run))
    # djhuey binds `enqueue` to its instance as it is imported: the bound method of the original.
    djhuey = sys.modules.get("huey.contrib.djhuey")
    if djhuey is not None and getattr(djhuey, "HUEY", None) is not None:
        setattr(djhuey, "enqueue", djhuey.HUEY.enqueue)  # noqa: B010 (a module's attribute)
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
