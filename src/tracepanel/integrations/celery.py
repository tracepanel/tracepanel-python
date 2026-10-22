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

Canvas workflows link up by themselves: the next step of a chain, a chord's callback and a `link` callback are sent
from inside the run before them, so their traces link to it (`enqueued_by`). Each run also records the ids Celery
gives it: `task.parent_id`, `task.root_id` and `task.group_id` (a group's or a chord's header tasks).

A run stopped by its soft time limit fails with SoftTimeLimitExceeded and says the limit (`task.timeout`). The hard
limit kills the pool process before the run can say anything: the worker's main process then writes the run's trace,
failed with TimeLimitExceeded, and says it finished. So it does for a run whose pool process was terminated (a cold
shutdown, `revoke(terminate=True)`: REVOKED with Terminated) or died (FAILURE with WorkerLostError). The pool process,
sent SIGTERM before it is killed, writes nothing of the run it leaves unfinished: one trace per run, the main
process's. A run Celery ends without a state (an error outside the task's body, as when a soft time limit hits Celery
importing its result backend) failed: FAILURE, with that error.

A soft time limit can also pass inside tracepanel's own task_prerun or task_postrun: Celery logs the exception and
goes on. What tracepanel began is undone or ended, so no trace stays current for the next run in the process, and the
run is still recorded: with SoftTimeLimitExceeded as a handled error and `task.timeout`. A run whose task_prerun
never reached tracepanel is recorded from its end, with the state Celery gives.
"""

import functools
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tracepanel.core import conf, errors, forks, registry, services
from tracepanel.core.context import (
    TraceBlock,
    adopt,
    current_span,
    current_trace,
    now_us,
    record_span,
    recorded,
    start_trace,
)
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import (
    Arguments,
    Heartbeat,
    TaskRun,
    Workflow,
    arguments,
    eta,
    event_repr,
    task_event,
    task_repr,
    trigger,
)
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
# Task id -> (when publishing started, the id the enqueue span will get, the trace's span count then, the message's
# priority): known before the message leaves, so the worker's link can point at that span, and what publishing
# recorded can go under it.
_published: dict[str, tuple[int, str, int, object]] = {}
# The exceptions a time limit stops a run with (billiard's).
TIME_LIMITS = frozenset({"SoftTimeLimitExceeded", "TimeLimitExceeded"})
_lock = forks.lock()
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
        properties = kwargs.get("properties")
        priority = properties.get("priority") if isinstance(properties, Mapping) else None
        with _lock:
            _published[task_id] = (now_us(), span_id, recorded(), priority)
    del sender


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
    start, span_id, mark, priority = published
    # The broker's commands while publishing (Redis LPUSH…) go under the enqueue span.
    adopt(span_id, mark)
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
            "task.eta": eta(headers.get("eta")),
            "task.retries": int(headers.get("retries") or 0),
            **({"task.priority": priority} if isinstance(priority, int) else {}),
            "code.origin": origin(),
            **_broker(_current_app()),
        },
        span_id=span_id,
    )


def _arguments(body: object, headers: Mapping[str, Any]) -> Arguments:
    """The arguments of a message: its body's (args, kwargs, embed) in Celery's protocol 2, a dict in protocol 1.

    Shown as Celery's argsrepr and kwargsrepr, which the message carries to the worker: the same text there.
    """
    args: object = None
    kwargs: object = None
    if isinstance(body, (tuple, list)) and len(body) >= 2:  # noqa: PLR2004 (args, kwargs)
        args, kwargs = body[0], body[1]
    elif isinstance(body, Mapping):
        args, kwargs = body.get("args"), body.get("kwargs")
    return arguments(args, kwargs, shown=(headers.get("argsrepr"), headers.get("kwargsrepr")))


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
    task_event(
        "enqueued",
        SYSTEM,
        task_id,
        name=name,
        queue=queue or None,
        retries=int(headers.get("retries") or 0),
        **(_arguments(body, headers).event() if config.capture_task_args else {}),
        trace_id=parent[0] if parent else None,
        span_id=parent[1] if parent else None,
    )


# ---------- the worker side ----------


def _running() -> list[str]:
    """The tasks this process runs now, eager ones left out (they are their caller's)."""
    with _lock:
        return [task_id for task_id, run in _runs.items() if not run.trace.attrs.get("task.eager")]


_heart = Heartbeat(SYSTEM, _running)


def _started(task_id: str, task: "Task[Any, Any]", attrs: Attrs, called: Arguments | None) -> None:
    hostname = str(attrs.get("task.worker") or "")
    _heart.start(hostname)
    task_event(
        "started",
        SYSTEM,
        task_id,
        name=task.name,
        queue=attrs.get("task.queue"),
        worker=hostname or None,
        retries=attrs.get("task.retries"),
        **(called.event() if called is not None else {}),
    )


def prerun(task_id: str | None = None, task: "Task[Any, Any] | None" = None, **kwargs: object) -> None:
    if task is None or task_id is None:
        return
    try:
        _prerun(task_id, task, kwargs)
    except BaseException as exc:
        _cut_short(task_id, task, exc)
        raise


def _prerun(task_id: str, task: "Task[Any, Any]", kwargs: Mapping[str, object]) -> None:
    if conf.matches(task.name, conf.get_config().ignore_tasks):
        return
    request = task.request
    eager = bool(getattr(request, "is_eager", False))
    if not eager:
        _leftover()
    started = now_us()
    attrs = _identity(task_id, task)
    # As the enqueued event showed them: Celery's argsrepr and kwargsrepr come with the message.
    called = (
        arguments(
            kwargs.get("args"),
            kwargs.get("kwargs"),
            shown=(getattr(request, "argsrepr", None), getattr(request, "kwargsrepr", None)),
        )
        if conf.get_config().capture_task_args
        else None
    )
    if not eager:
        attrs.update(_broker(task.app))
        _started(task_id, task, attrs, called)
    published = _header(request, PUBLISHED)
    if isinstance(published, int):
        attrs["task.queue_wait"] = max(started - published, 0)
    trigger = _header(request, TRIGGER)
    attrs["task.trigger"] = str(trigger) if trigger else ("request" if eager and current_trace() else "other")
    if called is not None:
        attrs.update(called.attrs())
    attrs.update(_scheduling(request))

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

    _begin(task_id, start_trace("task", task.name, attrs, root_kind="task.run"), links)


def _identity(task_id: str, task: Any) -> Attrs:  # noqa: ANN401 (a Celery task)
    """What a run's task and request say of it at once: enough for its trace when nothing more is known."""
    request = task.request
    return {
        "task.system": SYSTEM,
        "task.name": task.name,
        "task.id": task_id,
        "task.queue": str((getattr(request, "delivery_info", None) or {}).get("routing_key") or "") or None,
        "task.worker": str(getattr(request, "hostname", "") or "") or None,
        "task.retries": int(getattr(request, "retries", 0) or 0),
        "task.eager": bool(getattr(request, "is_eager", False)),
    }


def _begin(task_id: str, context: TraceBlock, links: list[Link]) -> TaskRun:
    """The run's trace made current and kept for its end, prerun's last step: undone if cut short before it is kept."""
    run: TaskRun | None = None
    try:
        trace = context.__enter__()
        trace.links.extend(links)
        run = TaskRun(context=context, trace=trace)
        with _lock:
            _runs[task_id] = run
    except BaseException:
        with _lock:
            kept = run is not None and _runs.get(task_id) is run
        if not kept:
            context.__exit__(None, None, None)
        raise
    return run


def _cut_short(task_id: str, task: "Task[Any, Any]", exc: BaseException) -> None:
    """Prerun was stopped halfway: by a soft time limit, whose signal can land anywhere, or by an error of its own.

    Celery's signal swallows the exception, logs it and runs the task: the run is recorded all the same. Its trace
    is the one prerun made, when it got as far as keeping it, else one from what the task says (_late). Nothing
    prerun began is left current to leak into the next run. The exception is kept on the trace, handled: Celery
    logged it, and the run's own outcome comes with its end.
    """
    with _lock:
        run = _runs.get(task_id)
    if run is None:
        run = _late(task_id, task)
        if run is None:
            return
        with _lock:
            _runs[task_id] = run
    status = run.trace.status
    errors.capture(exc, handled=True)
    run.trace.status = status
    _limited(run, exc, task)


def _late(task_id: str | None, task: object) -> TaskRun | None:
    """A trace begun now for a run prerun didn't start, from what its task says; None for a run not traced.

    Only for the run this process is running: its task's request is that run's (the worker's main process tells
    of a run its pool process ran, with no request of it).
    """
    name = getattr(task, "name", None)
    request = getattr(task, "request", None)
    if (
        not task_id
        or not isinstance(name, str)
        or getattr(request, "id", None) != task_id
        or conf.matches(name, conf.get_config().ignore_tasks)
    ):
        return None
    context = start_trace("task", name, _identity(task_id, task), root_kind="task.run")
    return TaskRun(context=context, trace=context.__enter__())


def _leftover() -> None:
    """A run's trace still current as the next run starts here: its end was cut short. It ends now, not current.

    A worker runs nothing inside a trace, so a task trace current here is an earlier run's.
    """
    stale = current_trace()
    if stale is None or stale.kind != "task":
        return
    with _lock:
        ids = [task_id for task_id, run in _runs.items() if run.trace is stale]
        runs = [_runs.pop(task_id) for task_id in ids]
    for run in runs:
        run.context.__exit__(None, None, None)


def _scheduling(request: object) -> Attrs:
    """When the run was due, its priority, and the workflow Celery's canvas put it in: those the request has."""
    attrs: Attrs = {}
    due = eta(getattr(request, "eta", None))
    if due:
        attrs["task.eta"] = due
    priority = (getattr(request, "delivery_info", None) or {}).get("priority")
    if isinstance(priority, int):
        attrs["task.priority"] = priority
    workflow = Workflow(
        parent_id=_text(getattr(request, "parent_id", None)),
        root_id=_text(getattr(request, "root_id", None)),
        group_id=_text(getattr(request, "group", None)),
    )
    attrs.update(workflow.attrs(str(getattr(request, "id", "") or "")))
    return attrs


def _text(value: object) -> str | None:
    return str(value) if value else None


def _soft_limit(task: object) -> object:
    """The soft time limit the run had: the message's, the task's, or the worker's default."""
    limits = getattr(getattr(task, "request", None), "timelimit", None)
    soft = limits[1] if isinstance(limits, (list, tuple)) and len(limits) == 2 else None  # noqa: PLR2004 (hard, soft)
    app_conf = getattr(getattr(task, "app", None), "conf", None)
    return soft or getattr(task, "soft_time_limit", None) or getattr(app_conf, "task_soft_time_limit", None)


def failure(
    sender: object = None, task_id: str | None = None, exception: BaseException | None = None, **kwargs: object
) -> None:
    del kwargs
    if exception is None:
        return
    with _lock:
        run = _runs.get(task_id or "")
    if run is None:
        # A run prerun never reached (a soft time limit hitting Celery before it): it is recorded from here.
        run = _late(task_id, sender)
        if run is None:
            return
        with _lock:
            _runs[task_id or ""] = run
    _failed(run, exception, sender)


def _failed(run: TaskRun, exception: BaseException, task: object) -> None:
    errors.capture(exception, handled=False)
    run.trace.status = "error"
    _limited(run, exception, task)


def _limited(run: TaskRun, exception: BaseException, task: object) -> None:
    """The soft time limit the run had (`task.timeout`), when a time limit stopped it."""
    if type(exception).__name__ in TIME_LIMITS:
        limit = _soft_limit(task)
        if isinstance(limit, (int, float)):
            run.trace.attrs["task.timeout"] = limit


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
    run = _ending(task_id, kwargs.get("task"))
    if run is None:
        return
    if state is None and _terminating():
        # Its pool process is being terminated: SIGTERM unwinds the run (SystemExit), which never finished. The
        # worker's main process writes its trace and says it finished (_ended): a second one from here would read ok.
        run.context.__exit__(None, None, None)
        return
    if state is None:
        state = _unfinished(run, kwargs.get("task"))
    trace = run.trace
    completed = False
    try:
        trace.attrs["task.state"] = state
        if state == "SUCCESS" and conf.get_config().capture_task_args:
            trace.attrs["task.result"] = task_repr("result", retval)
        if state == "FAILURE":
            trace.status = "error"
        run.context.__exit__(None, None, None)
        if state == "RETRY":
            trace.status = "ok"
        complete(trace)
        completed = True
        # A retry waits for its next attempt: it is still in flight.
        if state != "RETRY" and not trace.attrs.get("task.eager"):
            task_event("finished", SYSTEM, task_id or "", worker=trace.attrs.get("task.worker"), state=state)
    except BaseException:
        # Cut short too (a soft time limit as the run ends): the trace is still written, and not left current.
        run.context.__exit__(None, None, None)
        if not completed:
            complete(trace)
        raise


def _ending(task_id: str | None, task: object) -> TaskRun | None:
    """The run postrun ends: the one prerun began, or one recorded from its end when prerun never reached it."""
    with _lock:
        run = _runs.pop(task_id or "", None)
    return run if run is not None else _late(task_id, task)


def _unfinished(run: TaskRun, task: object) -> str:
    """The state of a run Celery gives none: it failed, with the exception unwinding it, as Celery itself reports it.

    Celery says no state when the run ends outside the task's body (storing STARTED, or a soft time limit hitting
    there), or when an eager task's exception propagates to its caller. Celery's task_failure comes after
    task_postrun then, or not at all: the exception is read here, while it unwinds the run.
    """
    exception = sys.exc_info()[1]
    if exception is not None:
        _failed(run, exception, task)
    run.trace.status = "error"
    return "FAILURE"


def _terminating() -> bool:
    """Whether this pool process is being terminated: billiard's SIGTERM handler fired, the run is unwinding."""
    try:
        from billiard import common  # noqa: PLC0415 (Celery's own dependency)
    except ImportError:
        return False
    fired = getattr(common, "terminate_handler_fired", None)
    if callable(fired):
        return bool(fired())
    # An older billiard keeps the flag without a function to read it.
    flag = getattr(common, "_should_have_exited", None)
    return bool(flag and flag[0])


def _on_timeout(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def on_timeout(self: Any, soft: bool, timeout: float) -> Any:  # noqa: ANN401, FBT001 (Celery's signature)
        result = original(self, soft, timeout)
        if not soft:
            from billiard.exceptions import TimeLimitExceeded  # noqa: PLC0415 (Celery's own dependency)

            _ended(self, TimeLimitExceeded(timeout), "FAILURE", timeout=timeout)
        return result

    return on_timeout


def _on_failure(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def on_failure(self: Any, exc_info: Any, *args: object, **kwargs: object) -> Any:  # noqa: ANN401 (Celery's)
        result = original(self, exc_info, *args, **kwargs)
        _lost(self, exc_info)
        return result

    return on_failure


def _lost(request: Any, exc_info: Any) -> None:  # noqa: ANN401 (a Request, billiard's ExceptionInfo)
    """The pool process running it was terminated or died: its trace never ended there, so it is written here."""
    from billiard.exceptions import Terminated, WorkerLostError  # noqa: PLC0415 (Celery's own dependency)

    exception = getattr(exc_info, "exception", None)
    # billiard's ExceptionInfo wraps it, with its traceback as text.
    exception = getattr(exception, "exc", exception)
    if isinstance(exception, Terminated):
        state = "REVOKED"
    elif isinstance(exception, WorkerLostError):
        state = "FAILURE"
    else:
        return
    with _lock:
        # Run in this process (the solo or threads pool): its own trace says how it ended.
        if str(getattr(request, "id", "") or "") in _runs:
            return
    _ended(request, exception, state)


def _ended(request: Any, error: BaseException, state: str, *, timeout: float | None = None) -> None:  # noqa: ANN401
    """The trace of a run whose pool process was killed, written by the worker's main process from its request."""
    name = str(getattr(request, "task_name", "") or getattr(request, "name", ""))
    task_id = str(getattr(request, "id", "") or "")
    if not task_id or conf.matches(name, conf.get_config().ignore_tasks):
        return
    headers: Mapping[str, Any] = getattr(request, "request_dict", None) or {}
    hostname = str(getattr(request, "hostname", "") or "") or None
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": name,
        "task.id": task_id,
        "task.queue": str((getattr(request, "delivery_info", None) or {}).get("routing_key") or "") or None,
        "task.worker": hostname,
        "task.retries": int(headers.get("retries") or 0),
        "task.eager": False,
        "task.trigger": str(headers.get(TRIGGER) or "other"),
        **_broker(getattr(request, "app", None)),
        **_scheduling(_Headers(headers)),
    }
    if conf.get_config().capture_task_args:
        attrs.update(
            arguments(
                getattr(request, "args", None),
                getattr(request, "kwargs", None),
                shown=(headers.get("argsrepr"), headers.get("kwargsrepr")),
            ).attrs()
        )
    started = getattr(request, "time_start", None)
    with start_trace("task", name, attrs, root_kind="task.run") as trace:
        parent = headers.get(PARENT)
        if isinstance(parent, str) and ":" in parent:
            parent_trace, parent_span = parent.split(":", 1)
            trace.links.append(Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span))
        errors.capture(error, handled=False)
    trace.attrs["task.state"] = state
    if timeout is not None:
        trace.attrs["task.timeout"] = timeout
    if isinstance(started, (int, float)) and started > 0:
        # It ran from when the pool process took it until now.
        trace.start = trace.spans[0].start = int(started * 1_000_000)
        trace.duration = trace.spans[0].duration = max(now_us() - trace.start, 0)
    complete(trace)
    task_event("finished", SYSTEM, task_id, worker=hostname, state=state)


class _Headers:
    """A message's headers read as a task's request (eta, delivery_info, parent_id…), for _scheduling."""

    def __init__(self, headers: Mapping[str, Any]) -> None:
        self.id = headers.get("id")
        self.eta = headers.get("eta")
        self.delivery_info = headers.get("delivery_info")
        self.parent_id = headers.get("parent_id")
        self.root_id = headers.get("root_id")
        self.group = headers.get("group")


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
    # No signal tells of the hard time limit: the request's handler does, in the worker's main process.
    from celery.worker.request import Request  # noqa: PLC0415

    if not patched(Request, "on_timeout"):
        patch(Request, "on_timeout", _on_timeout(Request.on_timeout))
    # Nor of a pool process terminated or lost: the request fails with Terminated or WorkerLostError there.
    if not patched(Request, "on_failure"):
        patch(Request, "on_failure", _on_failure(Request.on_failure))
