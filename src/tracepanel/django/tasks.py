"""Django Tasks (`django.tasks`, Django 6.0+; the `django_tasks` backport on 5.2): runs and enqueues as `task.*`.

Same model as Celery, `task.system = "django"`. Enqueue (`Task.enqueue` / `aenqueue`) is a `task.enqueue` span with
the result id; each run (the `task_started` / `task_finished` signals every backend sends) is a `task` trace.
Django Tasks carry no headers, so a worker's run can't name the span that enqueued it: the store links the two by
`task.id` when both are written, in whichever order. The ImmediateBackend runs the task inside `enqueue()`: that
run links to the enqueue span directly, like an eager Celery task.
"""

import functools
import sys
import threading
from contextvars import ContextVar
from typing import Any

from tracepanel.core import conf, errors
from tracepanel.core.context import current_trace, start_span, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, Link
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import TaskRun, task_repr, trigger
from tracepanel.storage.transport import complete, role

SYSTEM = "django"
STATES = {"SUCCESSFUL": "SUCCESS", "FAILED": "FAILURE"}
# Backend classes -> messaging.system (where the task waits for a worker); Immediate and Dummy have no queue.
BACKENDS = {"DatabaseBackend": "database", "RQBackend": "rq"}
NO_QUEUE = frozenset({"ImmediateBackend", "DummyBackend"})

_runs: dict[str, TaskRun] = {}
_lock = threading.Lock()
# (trace id, span id) of the enqueue in progress: a run that starts meanwhile runs inline (ImmediateBackend).
_enqueuing: ContextVar[tuple[str, str] | None] = ContextVar("tracepanel_django_task_enqueue", default=None)


def _enqueue_attrs(task: Any) -> Attrs:  # noqa: ANN401 (django.tasks.Task)
    kind = type(task.get_backend()).__name__
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": task.module_path,
        "task.queue": task.queue_name,
        "task.priority": task.priority,
        "task.backend": task.backend,
        "code.origin": origin(),
    }
    if kind not in NO_QUEUE:
        attrs["messaging.system"] = BACKENDS.get(kind, kind.lower())
    return attrs


def _wrap_enqueue(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def enqueue(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(self, *args, **kwargs)
        with start_span("task.enqueue", f"enqueue {self.module_path}", _enqueue_attrs(self)) as span:
            trace = current_trace()
            token = _enqueuing.set((trace.id, span.id) if span is not None and trace is not None else None)
            try:
                result = original(self, *args, **kwargs)
            finally:
                _enqueuing.reset(token)
            if span is not None:
                span.attrs["task.id"] = str(result.id)
            return result

    return enqueue


def _wrap_aenqueue(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    async def aenqueue(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return await original(self, *args, **kwargs)
        with start_span("task.enqueue", f"enqueue {self.module_path}", _enqueue_attrs(self)) as span:
            trace = current_trace()
            token = _enqueuing.set((trace.id, span.id) if span is not None and trace is not None else None)
            try:
                result = await original(self, *args, **kwargs)
            finally:
                _enqueuing.reset(token)
            if span is not None:
                span.attrs["task.id"] = str(result.id)
            return result

    return aenqueue


def _waited(result: Any) -> int | None:  # noqa: ANN401 (TaskResult)
    """Microseconds between enqueue and start, from the result's own timestamps."""
    if result.enqueued_at is None or result.started_at is None:
        return None
    return max(int((result.started_at - result.enqueued_at).total_seconds() * 1_000_000), 0)


def on_started(sender: object, task_result: Any = None, **kwargs: object) -> None:  # noqa: ANN401, ARG001 (signal API)
    if task_result is None:
        return
    task = task_result.task
    if conf.matches(task.module_path, conf.get_config().ignore_tasks):
        return
    inline = _enqueuing.get()
    caller = current_trace()
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": task.module_path,
        "task.id": str(task_result.id),
        "task.queue": task.queue_name,
        "task.backend": task_result.backend,
        "task.retries": max(task_result.attempts - 1, 0),
        "task.eager": inline is not None,
        "task.queue_wait": _waited(task_result),
    }
    if inline is not None:
        attrs["task.trigger"] = trigger(caller)
        # Runs in the caller's process: web or command, not a worker.
        attrs["service.role"] = role(caller) if caller is not None else "worker"
    if conf.get_config().capture_task_args:
        attrs["task.args"] = task_repr("args", task_result.args)
        attrs["task.kwargs"] = task_repr("kwargs", task_result.kwargs)
    context = start_trace("task", task.module_path, attrs, root_kind="task.run")
    trace = context.__enter__()
    if inline is not None:
        trace.links.append(Link(kind="enqueued_by", trace_id=inline[0], span_id=inline[1]))
    with _lock:
        _runs[str(task_result.id)] = TaskRun(context=context, trace=trace)


def on_finished(sender: object, task_result: Any = None, **kwargs: object) -> None:  # noqa: ANN401, ARG001
    if task_result is None:
        return
    with _lock:
        run = _runs.pop(str(task_result.id), None)
    if run is None:
        return
    trace = run.trace
    status = str(getattr(task_result.status, "value", task_result.status))
    trace.attrs["task.state"] = STATES.get(status, status)
    if status == "FAILED":
        # Backends send task_finished inside their except block: the exception is still current.
        exc = sys.exc_info()[1]
        if exc is not None:
            errors.capture(exc, handled=False)
        trace.status = "error"
    elif conf.get_config().capture_task_args:
        trace.attrs["task.result"] = task_repr("result", getattr(task_result, "_return_value", None))
    run.context.__exit__(None, None, None)
    complete(trace)


def _framework() -> Any:  # noqa: ANN401 (a module)
    """`django.tasks` on Django 6.0+, the `django_tasks` backport before it; None without either."""
    for name in ("django.tasks", "django_tasks"):
        try:
            base = __import__(f"{name}.base", fromlist=["Task"])
            signals = __import__(f"{name}.signals", fromlist=["task_started"])
        except ImportError:
            continue
        return base, signals
    return None


def install() -> None:
    """Instrument Django Tasks when available; a no-op otherwise."""
    found = _framework()
    if found is None:
        return
    base, signals = found
    if patched(base.Task, "enqueue"):
        return
    patch(base.Task, "enqueue", _wrap_enqueue(base.Task.enqueue))
    patch(base.Task, "aenqueue", _wrap_aenqueue(base.Task.aenqueue))
    signals.task_started.connect(on_started, dispatch_uid="tracepanel.django_tasks.started", weak=False)
    signals.task_finished.connect(on_finished, dispatch_uid="tracepanel.django_tasks.finished", weak=False)
