"""RQ: every job a worker performs is a `task` trace, every enqueue a `task.enqueue` span, and the two are linked.

RQ has no signals or middleware, so tracepanel wraps a few methods, turned on by themselves when RQ is installed:
`Queue.enqueue_job` (every way of enqueuing one job now ends there), `Queue.schedule_job` (`enqueue_at`,
`enqueue_in`) and `Queue.enqueue_many` (a batch, a Group) record the enqueue span and put a link to it in `job.meta`,
saved with the job; `Job.perform` (in the work horse, or inline for `Queue(is_async=False)`) runs the job as a trace
that links back to it. The default worker forks a horse per job and ends it with `os._exit`, so tracepanel writes each
job's trace before the horse goes away.

A job's trace says the worker that ran it (`task.worker`), when it was due (`task.eta`, for a scheduled job), the
job it waited for (`task.parent_id`, with every one in `task.depends_on`) or else the job whose run enqueued it, the
first job of that line (`task.root_id`) and its Group (`task.group_id`). A job stopped by its `job_timeout` fails with
JobTimeoutException and says the limit (`task.timeout`). Its `on_success`, `on_failure` and `on_stopped` callbacks run
as traces of their own, linked to the job's.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine.

A job's trace reaches the agent when it ends. Until then task events say where it is: `enqueued` from the enqueue
(with the enqueuing trace and span), `started` as `perform` begins, then `retried` (with the error) when RQ will run it
again, or `finished`. A worker says it is there when it registers (`register_birth`), and every process that runs jobs
(the worker, each work horse) sends a `heartbeat` with them, so a job whose horse was killed leaves the console's
Live list. A queue with `is_async=False` sends none: its jobs run inside the caller.
"""

import contextlib
import functools
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from tracepanel.core import conf, errors, forks, registry, services
from tracepanel.core.context import adopt, current_span, current_trace, now_us, record_span, recorded, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link, Trace
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import (
    Heartbeat,
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

SYSTEM = "rq"
# job.meta keys tracepanel adds: they are saved with the job and read in the worker.
PARENT = "tracepanel_parent"
TRIGGER = "tracepanel_trigger"
MAX_RETRIES = "tracepanel_max_retries"
# When a scheduled job is due, and the workflow it belongs to: the job whose run enqueued it, and that one's root.
ETA = "tracepanel_eta"
PARENT_TASK = "tracepanel_parent_task"
ROOT_TASK = "tracepanel_root_task"
# The callbacks a job may have: the attribute that holds each, as RQ's enqueue names it.
CALLBACKS = {"success": "on_success", "failure": "on_failure", "stopped": "on_stopped"}
# The jobs this process performs now, for its heartbeat.
_running: set[str] = set()
_lock = forks.lock()


def _running_ids() -> list[str]:
    with _lock:
        return sorted(_running)


_heart = Heartbeat(SYSTEM, _running_ids)
# True while a queue with is_async=False runs a job inside enqueue_job: that run is the caller's, never in flight.
_inline: ContextVar[bool] = ContextVar("tracepanel_rq_inline", default=False)
# The job this thread performed last and its trace's root span: its callbacks, run right after, link to it.
_performed: ContextVar[tuple[str, str, str] | None] = ContextVar("tracepanel_rq_performed", default=None)


def _broker(connection: Any) -> Attrs:  # noqa: ANN401 (redis.Redis)
    kwargs = getattr(getattr(connection, "connection_pool", None), "connection_kwargs", None)
    if isinstance(kwargs, dict) and kwargs.get("host"):
        return services.for_broker(f"redis://{kwargs['host']}:{kwargs.get('port', 6379)}")
    return {"messaging.system": "redis"}


# ---------- the enqueuing side ----------


def _meta(trace: Trace | None, span_id: str, job_id: str = "") -> dict[str, Any]:
    """What tracepanel saves in a new job's meta: how it was triggered, its enqueue span, the run that sent it."""
    meta: dict[str, Any] = {TRIGGER: trigger(trace)}
    if trace is not None:
        meta[PARENT] = f"{trace.id}:{span_id}"
    workflow = sent_from(trace, job_id)
    if workflow.parent_id:
        meta[PARENT_TASK] = workflow.parent_id
        meta[ROOT_TASK] = workflow.root_id
    return meta


def _enqueue(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def enqueue_job(self: Any, job: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        trace = current_trace()
        start, mark = now_us(), recorded()
        span_id = new_id()
        job.meta.update(_meta(trace, span_id, job.id))
        if job.retries_left is not None and MAX_RETRIES not in job.meta:
            job.meta[MAX_RETRIES] = job.retries_left
        inline = _inline.set(not getattr(self, "_is_async", True))
        try:
            result = original(self, job, *args, **kwargs)
        finally:
            _inline.reset(inline)
        _sent(self, job, trace, span_id, start, mark=mark)
        return result

    return enqueue_job


def _schedule(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def schedule_job(self: Any, job: Any, datetime: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        trace = current_trace()
        start, mark = now_us(), recorded()
        span_id = new_id()
        job.meta.update(_meta(trace, span_id, job.id))
        job.meta[ETA] = eta(datetime)
        if job.retries_left is not None and MAX_RETRIES not in job.meta:
            job.meta[MAX_RETRIES] = job.retries_left
        result = original(self, job, datetime, *args, **kwargs)
        _sent(self, job, trace, span_id, start, mark=mark)
        return result

    return schedule_job


def _enqueue_many(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def enqueue_many(self: Any, job_datas: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        trace = current_trace()
        start, mark = now_us(), recorded()
        stamped = []
        for data in job_datas:
            meta = {**(data.meta or {}), **_meta(trace, new_id())}
            if data.retry is not None:
                meta.setdefault(MAX_RETRIES, data.retry.max)
            stamped.append(data._replace(meta=meta))
        inline = _inline.set(not getattr(self, "_is_async", True))
        try:
            jobs = original(self, stamped, *args, **kwargs)
        finally:
            _inline.reset(inline)
        for index, job in enumerate(jobs):
            parent = str(job.meta.get(PARENT) or "")
            # One batch: its Redis commands go under the first job's enqueue span.
            _sent(self, job, trace, parent.partition(":")[2] or new_id(), start, mark=mark if index == 0 else None)
        return jobs

    return enqueue_many


def _sent(  # noqa: PLR0913 (what the enqueue knew)
    queue: Any,  # noqa: ANN401 (rq.Queue)
    job: Any,  # noqa: ANN401 (rq.job.Job)
    trace: Trace | None,
    span_id: str,
    start: int,
    *,
    mark: int | None,
) -> None:
    """After an enqueue: the `enqueued` event, and the enqueue span when it was sent inside a trace."""
    if getattr(queue, "_is_async", True):
        _enqueued(queue.name, job, (trace.id, span_id) if trace is not None else None)
    if trace is None:
        return
    if mark is not None:
        # RQ's Redis commands go under the enqueue span.
        adopt(span_id, mark)
    due = job.meta.get(ETA)
    record_span(
        "task.enqueue",
        f"enqueue {job.func_name}",
        start=start,
        duration=now_us() - start,
        attrs={
            "task.system": SYSTEM,
            "task.name": job.func_name,
            "task.id": job.id,
            "task.queue": queue.name,
            "task.eager": not getattr(queue, "_is_async", True),
            **({"task.eta": due} if due else {}),
            "code.origin": origin(),
            **_broker(queue.connection),
        },
        span_id=span_id,
    )


def _enqueued(queue: str, job: Any, parent: tuple[str, str] | None) -> None:  # noqa: ANN401 (rq.job.Job)
    """The job waits in its queue: not one that waits for another job first (deferred), or one ignored."""
    config = conf.get_config()
    status = job.get_status(refresh=False)
    if str(getattr(status, "value", status)) == "deferred" or conf.matches(str(job.func_name), config.ignore_tasks):
        return
    capture = config.capture_task_args
    task_event(
        "enqueued",
        SYSTEM,
        job.id,
        name=str(job.func_name),
        queue=queue,
        retries=_attempt(job),
        **(arguments(job.args, job.kwargs).event() if capture else {}),
        trace_id=parent[0] if parent else None,
        span_id=parent[1] if parent else None,
    )


# ---------- the worker side ----------


def _started(job: Any) -> None:  # noqa: ANN401 (rq.job.Job)
    worker = job.worker_name or None
    _heart.start(worker or "")
    with _lock:
        _running.add(job.id)
    capture = conf.get_config().capture_task_args
    task_event(
        "started",
        SYSTEM,
        job.id,
        name=str(job.func_name),
        queue=job.origin or None,
        worker=worker,
        retries=_attempt(job),
        **(arguments(job.args, job.kwargs).event() if capture else {}),
    )


def _ended(job: Any, state: str, failure: Exception | None) -> None:  # noqa: ANN401 (rq.job.Job)
    """`retried` when RQ runs the job again (its next attempt's count), else `finished`."""
    with _lock:
        _running.discard(job.id)
    worker = job.worker_name or None
    if state == "RETRY":
        error = event_repr("reason", failure) if failure is not None else None
        task_event("retried", SYSTEM, job.id, worker=worker, retries=_attempt(job) + 1, error=error)
    else:
        task_event("finished", SYSTEM, job.id, worker=worker, state=state)


def _attempt(job: Any) -> int:  # noqa: ANN401 (rq.job.Job)
    """How many times the job ran before: RQ only counts what is left."""
    most, left = job.meta.get(MAX_RETRIES), job.retries_left
    return max(int(most) - int(left), 0) if isinstance(most, int) and left is not None else 0


def _workflow(job: Any) -> Workflow:  # noqa: ANN401 (rq.job.Job)
    """The job it waited for (else the one whose run enqueued it), the first job of that line, its Group.

    The root of a job that waited for another is that one's root: read from the job it waited for, then kept in this
    job's meta for those that wait for it in turn.
    """
    dependencies = list(getattr(job, "_dependency_ids", None) or [])
    root = job.meta.get(ROOT_TASK)
    if dependencies and not root:
        try:
            dependency = job.dependency
        except Exception:  # noqa: BLE001 (gone from Redis: its id is all there is)
            dependency = None
        known = dependency.meta.get(ROOT_TASK) if dependency is not None else None
        root = known or dependencies[0]
        job.meta[ROOT_TASK] = root
        # Only the next jobs' root depends on it.
        with contextlib.suppress(Exception):
            job.save_meta()
    parent = dependencies[0] if dependencies else job.meta.get(PARENT_TASK)
    return Workflow(
        parent_id=str(parent) if parent else None,
        root_id=str(root) if root else None,
        group_id=str(job.group_id) if getattr(job, "group_id", None) else None,
    )


def _run_attrs(job: Any, *, eager: bool, started: int) -> Attrs:  # noqa: ANN401 (rq.job.Job)
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": str(job.func_name),
        "task.id": job.id,
        "task.queue": job.origin or None,
        "task.worker": job.worker_name or None,
        "task.retries": _attempt(job),
        "task.eager": eager,
        "task.trigger": str(job.meta.get(TRIGGER) or ("request" if eager else "other")),
        **_broker(job.connection),
    }
    due = job.meta.get(ETA)
    if due:
        attrs["task.eta"] = due
    dependencies = list(getattr(job, "_dependency_ids", None) or [])
    if dependencies:
        attrs["task.depends_on"] = dependencies
    attrs.update(_workflow(job).attrs(job.id))
    enqueued = job.enqueued_at
    if isinstance(enqueued, datetime) and not eager:
        at = enqueued if enqueued.tzinfo else enqueued.replace(tzinfo=UTC)
        attrs["task.queue_wait"] = max(started - int(at.timestamp() * 1_000_000), 0)
    caller = current_trace()
    if eager and caller is not None:
        # Runs inline, in the caller's process: web or command, not a worker.
        attrs["service.role"] = role(caller)
    if conf.get_config().capture_task_args:
        attrs.update(arguments(job.args, job.kwargs).attrs())
    return attrs


def _link(job: Any) -> Link | None:  # noqa: ANN401
    """The enqueue span: from job.meta, or the caller's span for a job run inline."""
    parent = job.meta.get(PARENT)
    if isinstance(parent, str) and ":" in parent:
        parent_trace, parent_span = parent.split(":", 1)
        return Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span)
    caller, span = current_trace(), current_span()
    return Link(kind="enqueued_by", trace_id=caller.id, span_id=span.id) if caller and span else None


def _perform(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def perform(self: Any) -> Any:  # noqa: ANN401
        if conf.matches(str(self.func_name), conf.get_config().ignore_tasks):
            return original(self)
        eager = current_trace() is not None
        attrs = _run_attrs(self, eager=eager, started=now_us())
        link = _link(self)
        in_flight = not (eager or _inline.get())
        if in_flight:
            _started(self)
        failure: Exception | None = None
        result: Any = None
        with start_trace("task", str(self.func_name), attrs, root_kind="task.run") as trace:
            if link is not None:
                trace.links.append(link)
            try:
                result = original(self)
            except Exception as exc:  # noqa: BLE001 (recorded, then raised again for RQ)
                failure = exc
                _failed(trace, self, exc)
            else:
                _succeeded(trace, result)
        complete(trace)
        _performed.set((self.id, trace.id, trace.spans[0].id))
        if in_flight:
            _ended(self, str(trace.attrs.get("task.state")), failure)
        if not eager:
            # The work horse ends with os._exit(): nothing at exit runs, so write the trace now.
            get_writer().flush()
        if failure is not None:
            raise failure
        return result

    return perform


def _succeeded(trace: Any, result: object) -> None:  # noqa: ANN401
    # A job may also ask for another run by returning rq.Retry.
    trace.attrs["task.state"] = "RETRY" if type(result).__name__ == "Retry" else "SUCCESS"
    if conf.get_config().capture_task_args:
        trace.attrs["task.result"] = task_repr("result", result)


def _failed(trace: Any, job: Any, exc: Exception) -> None:  # noqa: ANN401
    """RQ decides after the run: a job with retries left goes back to the queue, otherwise it failed."""
    retried = bool(job.should_retry)
    trace.attrs["task.state"] = "RETRY" if retried else "FAILURE"
    if retried:
        trace.attrs["task.retry_reason"] = task_repr("reason", exc)
    if type(exc).__name__ == "JobTimeoutException" and job.timeout:
        # Its job_timeout (the queue's default unless it set one) stopped it.
        trace.attrs["task.timeout"] = job.timeout
    errors.capture(exc, handled=retried)
    trace.status = "ok" if retried else "error"


# ---------- callbacks ----------


def _callback(kind: str, original: Any) -> Any:  # noqa: ANN401
    """`Job.execute_<kind>_callback`: the callback runs as a trace of its own, linked to the job's."""

    @functools.wraps(original)
    def execute(self: Any, *args: Any) -> Any:  # noqa: ANN401 (rq.job.Job; the death penalty, the outcome)
        try:
            callback = getattr(self, f"{kind}_callback", None)
        except Exception:  # noqa: BLE001 (one RQ can't import: RQ says so itself)
            callback = None
        name = _callable_name(callback)
        if callback is None or conf.matches(name, conf.get_config().ignore_tasks):
            return original(self, *args)
        root = self.meta.get(ROOT_TASK)
        attrs: Attrs = {
            "task.system": SYSTEM,
            "task.name": name,
            "task.queue": self.origin or None,
            "task.worker": self.worker_name or None,
            "task.eager": False,
            "task.trigger": "task",
            "task.callback": CALLBACKS[kind],
            "task.parent_id": self.id,
            "task.root_id": str(root) if root else self.id,
            **_broker(self.connection),
        }
        performed = _performed.get()
        failure: Exception | None = None
        result: Any = None
        with start_trace("task", name, attrs, root_kind="task.run") as trace:
            if performed is not None and performed[0] == self.id:
                # The job's trace, written a moment before in this process (not for on_stopped: its horse was killed).
                trace.links.append(Link(kind="enqueued_by", trace_id=performed[1], span_id=performed[2]))
            try:
                result = original(self, *args)
            except Exception as exc:  # noqa: BLE001 (recorded, then raised again for RQ)
                failure = exc
                errors.capture(exc, handled=False)
        trace.attrs["task.state"] = "FAILURE" if failure is not None else "SUCCESS"
        complete(trace)
        # A work horse ends with os._exit() right after.
        get_writer().flush()
        if failure is not None:
            raise failure
        return result

    return execute


def _callable_name(callback: object) -> str:
    module, qualname = getattr(callback, "__module__", None), getattr(callback, "__qualname__", None)
    return f"{module}.{qualname}" if module and qualname else repr(callback)


def _birth(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def register_birth(self: Any) -> Any:  # noqa: ANN401 (rq.worker.BaseWorker)
        result = original(self)
        # The worker is there before it runs anything: the console lists it among the workers.
        _heart.start(str(self.name))
        _heart.beat()
        return result

    return register_birth


def install() -> None:
    """Wrap RQ's enqueues, perform, the callbacks and a worker's start; a no-op without RQ."""
    try:
        from rq.job import Job  # noqa: PLC0415 (optional dependency)
        from rq.queue import Queue  # noqa: PLC0415
        from rq.worker import BaseWorker  # noqa: PLC0415
    except ImportError:
        return
    if patched(Queue, "enqueue_job"):
        return
    patch(Queue, "enqueue_job", _enqueue(Queue.enqueue_job))
    patch(Queue, "schedule_job", _schedule(Queue.schedule_job))
    patch(Queue, "enqueue_many", _enqueue_many(Queue.enqueue_many))
    patch(Job, "perform", _perform(Job.perform))
    for kind in CALLBACKS:
        method = f"execute_{kind}_callback"
        patch(Job, method, _callback(kind, getattr(Job, method)))
    patch(BaseWorker, "register_birth", _birth(BaseWorker.register_birth))
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
