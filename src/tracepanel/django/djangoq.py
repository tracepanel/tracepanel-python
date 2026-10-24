"""django-q2 (`django_q`): every task a cluster runs is a `task` trace, every enqueue a `task.enqueue` span, linked.

Same model as Celery, `task.system = "django-q"`. django-q's signals do most of it: `pre_enqueue` hands over the task
package before it is signed, so the current trace and span travel in it to the worker; `pre_execute` and
`post_execute_in_worker` open and close the run's trace in the worker process. tracepanel also wraps a few functions
of django-q's own: `async_task` (the enqueue span lasts as long as the push to the broker, and the `enqueued` event
follows it; a reference to async_task taken before tracepanel was installed still records both, at `pre_enqueue`),
`schedule` (a `task.enqueue` span with the first run's time, `task.eta`), the scheduler (what it enqueues is a
scheduled run, `task.trigger = "schedule"`), the timeout handler (the exception a run raised, which django-q keeps
only as text) and the monitor's `save_task` / `save_cached` (where a chain's next step is enqueued).

`sync=True` (or the `sync` setting) runs the task inside `async_task()`, like an eager Celery task: its trace links
to the enqueue span, says `task.eager`, and sends no task events.

A run says the worker process that ran it (`task.worker`, the name its events give), its cluster (`task.queue`), how
many runs of it came back before (`task.retries`: django-q's `attempt_count`; a broker that acknowledges delivers a
failed task again unless `ack_failure` is on) and how long it waited (`task.queue_wait`). A run stopped by its
`timeout` fails with TimeoutException and says the limit (`task.timeout`). The workflow follows django-q's: a task's
`group` is `task.group_id`, a chain's next step (`Chain`, `async_chain`) follows the step before it
(`task.parent_id`, `task.root_id` the chain's first step) and links to its run, and so does a task sent from inside
another's run. django-q gives a chain's steps a group id of their own to keep their results together: that is the
chain, not a group, so they have no `task.group_id`. A task's `hook` runs as a trace of its own
(`task.callback = "hook"`), linked to the task's run.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine.

A task's trace reaches the agent when its run ends. Until then task events say where it is: `enqueued` after the push
(with the enqueuing trace and span), `started` and `finished` around the run. Each worker process says it is there as
it spawns and sends a `heartbeat` with the task it runs, so a task whose worker was killed leaves the console's Live
list. The cluster says when a worker process is gone (`worker_stopped`): one it replaced (recycled, stopped by its
timeout, dead) and, as the cluster stops, each of its workers.
"""

import functools
import os
import socket
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tracepanel.core import conf, errors, forks, profile, registry
from tracepanel.core.context import current_trace, now_us, record_span, start_span, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs, Link, Span
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import (
    Heartbeat,
    TaskRun,
    Workflow,
    arguments,
    eta,
    sent_from,
    task_event,
    task_repr,
    trigger,
)
from tracepanel.storage import agent
from tracepanel.storage.transport import complete, get_writer, role

SYSTEM = "django-q"
# Keys tracepanel adds to the task package: signed and pickled with it, read in the worker and the monitor.
PARENT = "tracepanel_parent"
TRIGGER = "tracepanel_trigger"
PARENT_TASK = "tracepanel_parent_task"
ROOT_TASK = "tracepanel_root_task"
# Enqueued by a trace that asked to be profiled: the run is profiled too.
PROFILE = "tracepanel_profile"
# Added in the worker: the run's trace and root span, and the worker, for the hook and the chain's next step.
RUN = "tracepanel_run"
WORKER = "tracepanel_worker"
# Broker settings (django-q's Conf) -> messaging.system: where a task waits for a worker. Redis is the default.
BROKERS = (("ORM", "database"), ("MONGO", "mongodb"), ("SQS", "aws_sqs"), ("IRON_MQ", "ironmq"))

_runs: dict[str, TaskRun] = {}
_lock = forks.lock()


@dataclass(slots=True)
class _Sending:
    """An async_task() call in progress: its enqueue span (None outside a trace), and the package pre_enqueue saw."""

    span: Span | None
    task: dict[str, Any] = field(default_factory=dict)


# The async_task() call in progress in this thread.
_sending: ContextVar[_Sending | None] = ContextVar("tracepanel_django_q_sending", default=None)
# The finished task the monitor saves now: a chain's next step is enqueued meanwhile, and its hook runs.
_saving: ContextVar[dict[str, Any] | None] = ContextVar("tracepanel_django_q_saving", default=None)
# True while the scheduler runs: what it enqueues is a scheduled run.
_scheduling: ContextVar[bool] = ContextVar("tracepanel_django_q_scheduling", default=False)
# True while a task runs with sync=True: django-q runs a worker inside async_task(), which is not a worker process.
_inline: ContextVar[bool] = ContextVar("tracepanel_django_q_inline", default=False)


def _running() -> list[str]:
    with _lock:
        return [task_id for task_id, run in _runs.items() if not run.trace.attrs.get("task.eager")]


_heart = Heartbeat(SYSTEM, _running)


def _worker(pid: int | None = None) -> str:
    """A worker process, as its events name it: this one, or the cluster's worker of that pid."""
    return f"django-q@{socket.gethostname()}:{pid or os.getpid()}"


def _name(func: object) -> str:
    """The task's name as django-q shows it: the function's dotted path."""
    from django_q.utils import get_func_repr  # noqa: PLC0415 (optional dependency)

    return str(get_func_repr(func))


def _queue(task: dict[str, Any]) -> str:
    """The cluster the task goes to: its own `cluster`, else this one's (the broker's list key)."""
    from django_q.conf import Conf  # noqa: PLC0415

    return str(task.get("cluster") or Conf.CLUSTER_NAME)


def _broker() -> Attrs:
    from django_q.conf import Conf  # noqa: PLC0415

    if Conf.BROKER_CLASS:
        return {"messaging.system": str(Conf.BROKER_CLASS).rsplit(".", 1)[-1].lower()}
    for setting, system in BROKERS:
        if getattr(Conf, setting, None):
            return {"messaging.system": system}
    return {"messaging.system": "redis"}


def _ignored(task: dict[str, Any]) -> bool:
    return conf.matches(_name(task.get("func")), conf.get_config().ignore_tasks)


# ---------- the enqueuing side ----------


def _wrap_async_task(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def async_task(func: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        name = _name(func)
        attrs: Attrs = {"task.system": SYSTEM, "task.name": name, "code.origin": origin()}
        with start_span("task.enqueue", f"enqueue {name}", attrs) as span:
            sending = _Sending(span=span)
            token = _sending.set(sending)
            try:
                result = original(func, *args, **kwargs)
            finally:
                _sending.reset(token)
            # Pushed: the task waits in its queue now.
            trace = current_trace()
            _sent(sending.task, (trace.id, span.id) if trace is not None and span is not None else None)
            return result

    return async_task


def on_enqueue(sender: object = None, task: Any = None, **kwargs: object) -> None:  # noqa: ANN401, ARG001 (signal API)
    """Stamp the package with what the worker needs: the enqueue span, the trigger, the workflow."""
    if not isinstance(task, dict):
        return
    sending = _sending.get()
    trace = current_trace()
    saving = _saving.get()
    span_id = sending.span.id if sending is not None and sending.span is not None else new_id()
    if _scheduling.get():
        task[TRIGGER] = "schedule"
    else:
        task[TRIGGER] = "task" if saving is not None and trace is None else trigger(trace)
    if trace is not None:
        task[PARENT] = f"{trace.id}:{span_id}"
        profile.carry(task, PROFILE)
    elif saving is not None and saving.get(RUN):
        # A chain's next step, enqueued by the monitor: it links to the step before.
        task[PARENT] = saving[RUN]
    workflow = _following(saving) if saving is not None else sent_from(trace, str(task.get("id") or ""))
    if workflow.parent_id:
        task[PARENT_TASK] = workflow.parent_id
        task[ROOT_TASK] = workflow.root_id
    if sending is not None:
        # async_task() closes its span and sends the event after the push.
        sending.task = task
        if sending.span is not None:
            sending.span.attrs.update(_enqueue_attrs(task))
        return
    # A reference to async_task taken before tracepanel wrapped it: the span and the event now.
    if trace is not None:
        name = _name(task.get("func"))
        record_span(
            "task.enqueue",
            f"enqueue {name}",
            start=now_us(),
            duration=0,
            attrs={"task.system": SYSTEM, "task.name": name, "code.origin": origin(), **_enqueue_attrs(task)},
            span_id=span_id,
        )
    _sent(task, (trace.id, span_id) if trace is not None else None)


def _following(saving: dict[str, Any]) -> Workflow:
    """A chain's next step follows the step the monitor saves: the chain's first step is its root."""
    parent = str(saving.get("id") or "")
    return Workflow(parent_id=parent or None, root_id=str(saving.get(ROOT_TASK) or parent) or None)


def _enqueue_attrs(task: dict[str, Any]) -> Attrs:
    attrs: Attrs = {
        "task.id": str(task.get("id") or ""),
        "task.queue": _queue(task),
        "task.eager": bool(task.get("sync")),
    }
    if not task.get("sync"):
        attrs.update(_broker())
    return attrs


def _sent(task: dict[str, Any], parent: tuple[str, str] | None) -> None:
    """The `enqueued` event, with the enqueuing trace and span: not for a task run inline (sync) or ignored."""
    if not task or task.get("sync") or _ignored(task):
        return
    task_event(
        "enqueued",
        SYSTEM,
        str(task.get("id") or ""),
        name=_name(task.get("func")),
        queue=_queue(task),
        retries=0,
        **(arguments(task.get("args"), task.get("kwargs")).event() if conf.get_config().capture_task_args else {}),
        trace_id=parent[0] if parent else None,
        span_id=parent[1] if parent else None,
    )


def _wrap_schedule(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def schedule(func: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(func, *args, **kwargs)
        name = _name(func)
        attrs: Attrs = {"task.system": SYSTEM, "task.name": name, "code.origin": origin()}
        with start_span("task.enqueue", f"enqueue {name}", attrs) as span:
            created = original(func, *args, **kwargs)
            if span is not None:
                due = eta(getattr(created, "next_run", None))
                span.attrs.update(
                    {
                        "task.queue": _queue({"cluster": getattr(created, "cluster", None)}),
                        "task.eager": False,
                        **({"task.eta": due} if due else {}),
                        **_broker(),
                    }
                )
            return created

    return schedule


def _wrap_scheduler(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def scheduler(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        token = _scheduling.set(True)
        try:
            return original(*args, **kwargs)
        finally:
            _scheduling.reset(token)

    return scheduler


def _wrap_sync(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def run_inline(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        token = _inline.set(True)
        try:
            return original(*args, **kwargs)
        finally:
            _inline.reset(token)

    return run_inline


# ---------- the worker side ----------


def on_spawn(sender: object = None, **kwargs: object) -> None:
    """A worker process says it is there before it runs anything: the console lists it among the workers."""
    del sender, kwargs
    if _inline.get():
        return
    _heart.start(_worker())
    _heart.beat()


def _attempts(task: dict[str, Any]) -> int:
    """How many runs of the task came back before this one: django-q counts them on its saved result."""
    if task.get("sync") or task.get("cached"):
        return 0
    from django_q.models import Task  # noqa: PLC0415

    try:
        count = Task.objects.filter(id=task.get("id")).values_list("attempt_count", flat=True).first()
    except Exception:  # noqa: BLE001 (no result table: none came back)
        return 0
    return int(count or 0)


def _chained(task: dict[str, Any]) -> bool:
    """A step of a chain: django-q hands each one the steps after it (none for the last) and the chain's group."""
    return "chain" in task


def _run_attrs(task: dict[str, Any], *, eager: bool, worker: str | None) -> Attrs:
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": _name(task.get("func")),
        "task.id": str(task.get("id") or ""),
        "task.queue": _queue(task),
        **({"task.worker": worker} if worker else {}),
        "task.retries": _attempts(task),
        "task.eager": eager,
        "task.trigger": str(task.get(TRIGGER) or ("request" if eager else "other")),
    }
    if not eager:
        attrs.update(_broker())
        enqueued = task.get("started")
        if isinstance(enqueued, datetime):
            # django-q stamps the package with the time it was enqueued.
            attrs["task.queue_wait"] = max(now_us() - int(enqueued.timestamp() * 1_000_000), 0)
    caller = current_trace()
    if eager and caller is not None:
        # Runs inline, in the caller's process: web or command, not a worker.
        attrs["service.role"] = role(caller)
    parent, root = task.get(PARENT_TASK), task.get(ROOT_TASK)
    workflow = Workflow(
        parent_id=str(parent) if parent else None,
        root_id=str(root) if root else None,
        group_id=None if _chained(task) or not task.get("group") else str(task["group"]),
    )
    attrs.update(workflow.attrs(str(task.get("id") or "")))
    if conf.get_config().capture_task_args:
        attrs.update(arguments(task.get("args"), task.get("kwargs")).attrs())
    return attrs


def on_execute(sender: object = None, func: object = None, task: Any = None, **kwargs: object) -> None:  # noqa: ANN401, ARG001
    if not isinstance(task, dict) or _ignored(task):
        return
    eager = bool(task.get("sync"))
    worker = None if eager else _worker()
    attrs = _run_attrs(task, eager=eager, worker=worker)
    name = str(attrs["task.name"])
    asked = profile.asked_by_parent(task.get(PROFILE))
    context = start_trace("task", name, attrs, root_kind="task.run", profile=asked)
    trace = context.__enter__()
    parent = task.get(PARENT)
    if isinstance(parent, str) and ":" in parent:
        parent_trace, parent_span = parent.split(":", 1)
        trace.links.append(Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span))
    task_id = str(task.get("id") or "")
    with _lock:
        _runs[task_id] = TaskRun(context=context, trace=trace)
    # The monitor (another process) runs its hook and enqueues its chain's next step: they link to this run.
    task[RUN] = f"{trace.id}:{trace.spans[0].id}"
    if worker is not None:
        task[WORKER] = worker
        _started(task, attrs, worker)


def _started(task: dict[str, Any], attrs: Attrs, worker: str) -> None:
    _heart.start(worker)
    task_event(
        "started",
        SYSTEM,
        str(attrs["task.id"]),
        name=attrs["task.name"],
        queue=attrs["task.queue"],
        worker=worker,
        retries=attrs["task.retries"],
        **(arguments(task.get("args"), task.get("kwargs")).event() if conf.get_config().capture_task_args else {}),
    )


def _wrap_timeout_exit(original: Any) -> Any:  # noqa: ANN401
    """The run's exception as it leaves the task: django-q catches it and keeps only its text."""

    @functools.wraps(original)
    def __exit__(self: Any, exc_type: Any, exc_value: Any, traceback: Any) -> Any:  # noqa: ANN401, N807
        trace = current_trace()
        if isinstance(exc_value, BaseException) and trace is not None and trace.attrs.get("task.system") == SYSTEM:
            errors.capture(exc_value, handled=False)
            limit = getattr(self, "_timeout", None)
            if type(exc_value).__name__ == "TimeoutException" and isinstance(limit, (int, float)) and limit > 0:
                trace.attrs["task.timeout"] = limit
        return original(self, exc_type, exc_value, traceback)

    return __exit__


def on_executed(sender: object = None, func: object = None, task: Any = None, **kwargs: object) -> None:  # noqa: ANN401, ARG001
    if not isinstance(task, dict):
        return
    task_id = str(task.get("id") or "")
    with _lock:
        run = _runs.pop(task_id, None)
    if run is None:
        return
    trace = run.trace
    # With sync=True, a failed run never gets its result: django-q raises it to the caller.
    succeeded = bool(task.get("success", False))
    trace.attrs["task.state"] = "SUCCESS" if succeeded else "FAILURE"
    if succeeded and conf.get_config().capture_task_args:
        trace.attrs["task.result"] = task_repr("result", task.get("result"))
    if not succeeded:
        trace.status = "error"
    run.context.__exit__(None, None, None)
    complete(trace)
    if trace.attrs.get("task.eager"):
        return
    task_event("finished", SYSTEM, task_id, worker=trace.attrs.get("task.worker"), state=trace.attrs["task.state"])
    # The worker process may end right after (recycled, or stopped by its timeout) without running anything at exit.
    get_writer().flush()


# ---------- the monitor: chains and hooks ----------


def _wrap_save(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def save(task: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        token = _saving.set(task if isinstance(task, dict) else None)
        try:
            return original(task, *args, **kwargs)
        finally:
            _saving.reset(token)

    return save


def _resolve(hook: object) -> Any:  # noqa: ANN401
    """The hook's function, as django-q's call_hook finds it; None when it can't."""
    if callable(hook):
        return hook
    if not isinstance(hook, str) or "." not in hook:
        return None
    import importlib  # noqa: PLC0415

    module, name = hook.rsplit(".", 1)
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None


def _call_hook(original: Any) -> Any:  # noqa: ANN401
    """django-q's post_save receiver that runs a task's hook: the hook runs as a trace of its own."""

    def call_hook(sender: Any, instance: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        hook = getattr(instance, "hook", None)
        func = _resolve(hook) if hook else None
        if func is None or conf.matches(_name(func), conf.get_config().ignore_tasks):
            return original(sender, instance, **kwargs)
        saving = _saving.get() or {}
        name = _name(func)
        root = saving.get(ROOT_TASK)
        eager = bool(saving.get("sync"))
        attrs: Attrs = {
            "task.system": SYSTEM,
            "task.name": name,
            "task.queue": str(getattr(instance, "cluster", None) or "") or None,
            "task.worker": saving.get(WORKER),
            "task.eager": eager,
            "task.trigger": "task",
            "task.callback": "hook",
            "task.parent_id": str(instance.id),
            "task.root_id": str(root) if root else str(instance.id),
        }
        failure: list[BaseException] = []

        def traced(task: Any) -> Any:  # noqa: ANN401
            try:
                return func(task)
            except Exception as exc:
                failure.append(exc)
                errors.capture(exc, handled=False)
                raise

        with start_trace("task", name, attrs, root_kind="task.run") as trace:
            run = saving.get(RUN)
            if isinstance(run, str) and ":" in run:
                run_trace, run_span = run.split(":", 1)
                trace.links.append(Link(kind="enqueued_by", trace_id=run_trace, span_id=run_span))
            instance.hook = traced
            try:
                # django-q's own receiver calls it, and logs what it raises.
                result = original(sender, instance, **kwargs)
            finally:
                instance.hook = hook
        trace.attrs["task.state"] = "FAILURE" if failure else "SUCCESS"
        complete(trace)
        if not eager:
            get_writer().flush()
        return result

    return call_hook


# ---------- the cluster: workers that stop ----------


def _wrap_reincarnate(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def reincarnate(self: Any, process: Any) -> Any:  # noqa: ANN401 (django_q.cluster.Sentinel, a Process)
        worker = process in self.pool
        try:
            return original(self, process)
        finally:
            # Replaced: recycled, stopped by its timeout, or dead. Its successor says it is there as it spawns.
            if worker:
                _heart.stopped(_worker(process.pid))

    return reincarnate


def _wrap_stop(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def stop(self: Any) -> Any:  # noqa: ANN401 (django_q.cluster.Sentinel)
        pids = [process.pid for process in self.pool]
        try:
            return original(self)
        finally:
            for pid in pids:
                _heart.stopped(_worker(pid))

    return stop


# ---------- installing ----------


def _patch_tasks(module: Any) -> None:  # noqa: ANN401 (django_q.tasks)
    if patched(module, "async_task"):
        return
    patch(module, "async_task", _wrap_async_task(module.async_task))
    patch(module, "schedule", _wrap_schedule(module.schedule))
    patch(module, "_sync", _wrap_sync(module._sync))  # noqa: SLF001 (async_task calls it for sync=True)


def _patch_signals(module: Any) -> None:  # noqa: ANN401 (django_q.signals)
    from django.db.models.signals import post_save  # noqa: PLC0415
    from django_q.models import Task  # noqa: PLC0415

    module.pre_enqueue.connect(on_enqueue, dispatch_uid="tracepanel.django_q.enqueue", weak=False)
    module.pre_execute.connect(on_execute, dispatch_uid="tracepanel.django_q.execute", weak=False)
    module.post_execute_in_worker.connect(on_executed, dispatch_uid="tracepanel.django_q.executed", weak=False)
    module.post_spawn.connect(on_spawn, dispatch_uid="tracepanel.django_q.spawn", weak=False)
    # The hook's receiver, replaced by one that runs it as a trace.
    if post_save.disconnect(module.call_hook, sender=Task):
        post_save.connect(
            _call_hook(module.call_hook), sender=Task, dispatch_uid="tracepanel.django_q.hook", weak=False
        )


def _patch_monitor(module: Any) -> None:  # noqa: ANN401 (django_q.monitor)
    if patched(module, "save_task"):
        return
    patch(module, "save_task", _wrap_save(module.save_task))
    patch(module, "save_cached", _wrap_save(module.save_cached))


def _patch_scheduler(module: Any) -> None:  # noqa: ANN401 (django_q.scheduler)
    patch(module, "scheduler", _wrap_scheduler(module.scheduler))


def _patch_timeout(module: Any) -> None:  # noqa: ANN401 (django_q.timeout)
    handler = module.TimeoutHandler
    if not patched(handler, "__exit__"):
        patch(handler, "__exit__", _wrap_timeout_exit(handler.__exit__))


def _patch_cluster(module: Any) -> None:  # noqa: ANN401 (django_q.cluster)
    sentinel = module.Sentinel
    if patched(sentinel, "reincarnate"):
        return
    patch(sentinel, "reincarnate", _wrap_reincarnate(sentinel.reincarnate))
    patch(sentinel, "stop", _wrap_stop(sentinel.stop))


def install() -> None:
    """Instrument django-q2 as each of its modules is imported (most need Django's apps ready); a no-op without it."""
    when_imported("django_q.signals", _patch_signals)
    when_imported("django_q.tasks", _patch_tasks)
    when_imported("django_q.monitor", _patch_monitor)
    when_imported("django_q.scheduler", _patch_scheduler)
    when_imported("django_q.timeout", _patch_timeout)
    when_imported("django_q.cluster", _patch_cluster)
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
