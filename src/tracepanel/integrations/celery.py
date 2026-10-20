"""Celery: every task run is a `task` trace, every enqueue a `task.enqueue` span, and the two are linked.

Turned on by itself when Celery is installed (signals only, nothing to configure). In the process that enqueues,
`before_task_publish` adds the current trace and span to the message headers; in the worker, `task_prerun` starts a
trace that links back to them (`enqueued_by`) and measures how long the message waited in the queue. Inside the
run, SQL, cache, logs, mail and signals land in the task's trace and issues are found like for requests.

Eager tasks (`task_always_eager`, `.apply()`) run inside the caller: their trace links to the caller's span directly.
Task arguments and results are kept for local debugging only (masked like locals) and never leave the machine.

A task's trace reaches the agent when the task ends. Until then task events say where it is: `enqueued` after
publishing (with the enqueuing trace and span), `started` in `task_prerun`, `retried` in `task_retry`, `finished` in
`task_postrun`; each worker process sends a `heartbeat` with the tasks it runs every HEARTBEAT_S, so a task whose
process was killed leaves the console's Live list. Eager tasks send none: they run inside their caller.
"""

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tracepanel.core import conf, errors, registry, services
from tracepanel.core.context import adopt, current_span, current_trace, now_us, record_span, recorded, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link
from tracepanel.integrations._tasks import Heartbeat, TaskRun, event_repr, task_event, task_repr, trigger
from tracepanel.storage import agent
from tracepanel.storage.transport import complete, get_writer, role

if TYPE_CHECKING:
    from celery import Task

# task.* attributes are shared by every task queue integration; task.system says which one ran it.
SYSTEM = "celery"
PARENT = "tracepanel_parent"
PUBLISHED = "tracepanel_published"
TRIGGER = "tracepanel_trigger"
_runs: dict[str, TaskRun] = {}
# Task id -> (when publishing started, the id the enqueue span will get, the trace's span count then): known before
# the message leaves, so the worker's link can point at that span, and what publishing recorded can go under it.
_published: dict[str, tuple[int, str, int]] = {}
_lock = threading.Lock()
_beat = False


def _header(request: object, name: str) -> object:
    """A custom message header: Celery exposes them on the request, older protocols under `headers`."""
    value = getattr(request, name, None)
    if value is None:
        headers = getattr(request, "headers", None)
        if isinstance(headers, Mapping):
            value = headers.get(name)
    return value


def _current_app() -> object:
    from celery import current_app  # noqa: PLC0415 (optional dependency)

    return current_app


def _broker(app: object) -> Attrs:
    """The broker this app publishes to and consumes from (Celery's default is a local RabbitMQ)."""
    settings = getattr(app, "conf", None)
    url = getattr(settings, "broker_write_url", None) or getattr(settings, "broker_url", None)
    if isinstance(url, (list, tuple)):
        url = url[0] if url else None
    return services.for_broker(str(url or ""))


def _trigger() -> str:
    return "schedule" if _beat else trigger(current_trace())


# ---------- the enqueuing side ----------


def before_publish(sender: str | None = None, headers: dict[str, Any] | None = None, **kwargs: object) -> None:
    if headers is None:
        return
    task_id = str(headers.get("id", ""))
    headers[PUBLISHED] = now_us()
    headers[TRIGGER] = _trigger()
    trace = current_trace()
    if trace is not None:
        span_id = new_id()
        headers[PARENT] = f"{trace.id}:{span_id}"
        with _lock:
            _published[task_id] = (now_us(), span_id, recorded())
    del sender, kwargs


def after_publish(
    sender: str | None = None,
    headers: Mapping[str, Any] | None = None,
    routing_key: str | None = None,
    **kwargs: object,
) -> None:
    if headers is None:
        return
    task_id = str(headers.get("id", ""))
    with _lock:
        published = _published.pop(task_id, None)
    trace = current_trace()
    # The enqueue span's trace and id, when it is sent inside a trace: the console links the queued task to it.
    parent = (trace.id, published[1]) if published is not None and trace is not None else None
    _enqueued(str(sender), task_id, headers=headers, queue=routing_key, body=kwargs.get("body"), parent=parent)
    if published is None or trace is None:
        return
    start, span_id, mark = published
    # The broker's commands while publishing (Redis LPUSH…) go under the enqueue span.
    adopt(span_id, mark)
    eta = headers.get("eta")
    record_span(
        "task.enqueue",
        f"enqueue {sender}",
        start=start,
        duration=now_us() - start,
        attrs={
            "task.system": SYSTEM,
            "task.name": str(sender),
            "task.id": task_id,
            "task.queue": routing_key or None,
            "task.eta": str(eta) if eta else None,
            "task.retries": int(headers.get("retries") or 0),
            "code.origin": origin(),
            **_broker(_current_app()),
        },
        span_id=span_id,
    )


def _arguments(body: object) -> tuple[object, object]:
    """The args and kwargs of a message's body: (args, kwargs, embed) in Celery's protocol 2, a dict in protocol 1."""
    if isinstance(body, (tuple, list)) and len(body) >= 2:  # noqa: PLR2004 (args, kwargs)
        return body[0], body[1]
    if isinstance(body, Mapping):
        return body.get("args"), body.get("kwargs")
    return None, None


def _enqueued(  # noqa: PLR0913 (what the event tells)
    name: str,
    task_id: str,
    *,
    headers: Mapping[str, Any],
    queue: str | None,
    body: object,
    parent: tuple[str, str] | None,
) -> None:
    config = conf.get_config()
    if not task_id or conf.matches(name, config.ignore_tasks):
        return
    args, kwargs = _arguments(body) if config.capture_task_args else (None, None)
    task_event(
        "enqueued",
        SYSTEM,
        task_id,
        name=name,
        queue=queue or None,
        retries=int(headers.get("retries") or 0),
        args=event_repr("args", args) if args is not None else None,
        kwargs=event_repr("kwargs", kwargs) if kwargs is not None else None,
        trace_id=parent[0] if parent else None,
        span_id=parent[1] if parent else None,
    )


# ---------- the worker side ----------


def _running() -> list[str]:
    """The tasks this process runs now, eager ones left out (they are their caller's)."""
    with _lock:
        return [task_id for task_id, run in _runs.items() if not run.trace.attrs.get("task.eager")]


_heart = Heartbeat(SYSTEM, _running)


def _started(task_id: str, task: "Task[Any, Any]", attrs: Attrs, kwargs: Mapping[str, object]) -> None:
    hostname = str(attrs.get("task.worker") or "")
    _heart.start(hostname)
    capture = conf.get_config().capture_task_args
    task_event(
        "started",
        SYSTEM,
        task_id,
        name=task.name,
        queue=attrs.get("task.queue"),
        worker=hostname or None,
        retries=attrs.get("task.retries"),
        args=event_repr("args", kwargs.get("args")) if capture else None,
        kwargs=event_repr("kwargs", kwargs.get("kwargs")) if capture else None,
    )


def prerun(task_id: str | None = None, task: "Task[Any, Any] | None" = None, **kwargs: object) -> None:
    if task is None or task_id is None or conf.matches(task.name, conf.get_config().ignore_tasks):
        return
    request = task.request
    eager = bool(getattr(request, "is_eager", False))
    started = now_us()
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": task.name,
        "task.id": task_id,
        "task.queue": str((getattr(request, "delivery_info", None) or {}).get("routing_key") or "") or None,
        "task.worker": str(getattr(request, "hostname", "") or "") or None,
        "task.retries": int(getattr(request, "retries", 0) or 0),
        "task.eager": eager,
    }
    if not eager:
        attrs.update(_broker(task.app))
        _started(task_id, task, attrs, kwargs)
    published = _header(request, PUBLISHED)
    if isinstance(published, int):
        attrs["task.queue_wait"] = max(started - published, 0)
    trigger = _header(request, TRIGGER)
    attrs["task.trigger"] = str(trigger) if trigger else ("request" if eager and current_trace() else "other")
    if conf.get_config().capture_task_args:
        attrs["task.args"] = task_repr("args", kwargs.get("args"))
        attrs["task.kwargs"] = task_repr("kwargs", kwargs.get("kwargs"))

    links: list[Link] = []
    parent = _header(request, PARENT)
    caller, caller_span = current_trace(), current_span()
    if eager and caller is not None:
        # Runs inline, in the caller's process: web or command, not a worker.
        attrs["service.role"] = role(caller)
    if isinstance(parent, str) and ":" in parent:
        parent_trace, parent_span = parent.split(":", 1)
        links.append(Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span))
    elif eager and caller is not None and caller_span is not None:
        # Runs inline: the caller's trace gets the enqueue span, the task its own trace.
        span = record_span(
            "task.enqueue",
            f"enqueue {task.name}",
            start=started,
            duration=0,
            attrs={"task.system": SYSTEM, "task.name": task.name, "task.id": task_id, "task.eager": True},
        )
        links.append(Link(kind="enqueued_by", trace_id=caller.id, span_id=span.id if span else caller_span.id))

    context = start_trace("task", task.name, attrs, root_kind="task.run")
    trace = context.__enter__()
    trace.links.extend(links)
    with _lock:
        _runs[task_id] = TaskRun(context=context, trace=trace)


def failure(task_id: str | None = None, exception: BaseException | None = None, **kwargs: object) -> None:
    del kwargs
    run = _runs.get(task_id or "")
    if run is None or exception is None:
        return
    errors.capture(exception, handled=False)
    run.trace.status = "error"


def retry(request: object = None, reason: object = None, **kwargs: object) -> None:
    del kwargs
    run = _runs.get(str(getattr(request, "id", "")))
    if run is None:
        return
    # Celery wraps the cause in Retry(exc=...): report the cause.
    cause = getattr(reason, "exc", None) or reason
    run.trace.attrs["task.retry_reason"] = task_repr("reason", cause)
    if not run.trace.attrs.get("task.eager"):
        task_event(
            "retried",
            SYSTEM,
            str(getattr(request, "id", "")),
            worker=run.trace.attrs.get("task.worker"),
            # This retry counted: the next attempt's.
            retries=int(getattr(request, "retries", 0) or 0) + 1,
            error=event_repr("reason", cause),
        )
    if isinstance(cause, BaseException):
        # Retried, so handled: the next attempt is its own trace.
        errors.capture(cause, handled=True)


def postrun(task_id: str | None = None, retval: object = None, state: str | None = None, **kwargs: object) -> None:
    del kwargs
    with _lock:
        run = _runs.pop(task_id or "", None)
    if run is None:
        return
    trace = run.trace
    trace.attrs["task.state"] = state
    if state == "SUCCESS" and conf.get_config().capture_task_args:
        trace.attrs["task.result"] = task_repr("result", retval)
    if state == "FAILURE":
        trace.status = "error"
    run.context.__exit__(None, None, None)
    if state == "RETRY":
        trace.status = "ok"
    complete(trace)
    # A retry waits for its next attempt: it is still in flight.
    if state != "RETRY" and not trace.attrs.get("task.eager"):
        task_event("finished", SYSTEM, task_id or "", worker=trace.attrs.get("task.worker"), state=state)


def on_beat_init(**kwargs: object) -> None:
    """Everything beat publishes is a scheduled run."""
    del kwargs
    global _beat  # noqa: PLW0603 (a per-process mode flag)
    _beat = True


def on_worker_ready(sender: object = None, **kwargs: object) -> None:
    """The worker says it is there before it runs anything: the console lists it among the workers."""
    del kwargs
    _heart.worker = str(getattr(sender, "hostname", "") or "") or _heart.worker
    _heart.beat()


def on_worker_shutdown(**kwargs: object) -> None:
    """Write what is queued before the worker process goes away."""
    del kwargs
    get_writer().flush()


def install() -> None:
    """Connect to Celery's signals when Celery is installed; a no-op otherwise."""
    try:
        from celery import signals  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    signals.before_task_publish.connect(before_publish, dispatch_uid="tracepanel.before_publish", weak=False)
    signals.after_task_publish.connect(after_publish, dispatch_uid="tracepanel.after_publish", weak=False)
    signals.task_prerun.connect(prerun, dispatch_uid="tracepanel.prerun", weak=False)
    signals.task_failure.connect(failure, dispatch_uid="tracepanel.failure", weak=False)
    signals.task_retry.connect(retry, dispatch_uid="tracepanel.retry", weak=False)
    signals.task_postrun.connect(postrun, dispatch_uid="tracepanel.postrun", weak=False)
    signals.beat_init.connect(on_beat_init, dispatch_uid="tracepanel.beat_init", weak=False)
    signals.worker_ready.connect(on_worker_ready, dispatch_uid="tracepanel.worker_ready", weak=False)
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of task code: tracepanel sees them however the worker sets up logging.
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
    signals.worker_process_shutdown.connect(on_worker_shutdown, dispatch_uid="tracepanel.worker_shutdown", weak=False)
    signals.worker_shutdown.connect(on_worker_shutdown, dispatch_uid="tracepanel.worker_shutdown_main", weak=False)
