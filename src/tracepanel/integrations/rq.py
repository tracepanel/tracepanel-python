"""RQ: every job a worker performs is a `task` trace, every enqueue a `task.enqueue` span, and the two are linked.

RQ has no signals or middleware, so tracepanel wraps two methods, turned on by themselves when RQ is installed:
`Queue.enqueue_job` (every way of enqueuing ends there) records the enqueue span and puts a link to it in
`job.meta`, saved with the job; `Job.perform` (in the work horse, or inline for `Queue(is_async=False)`) runs the job
as a trace that links back to it. The default worker forks a horse per job and ends it with `os._exit`, so tracepanel
writes each job's trace before the horse goes away.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine.

A job's trace reaches the agent when it ends. Until then task events say where it is: `enqueued` from `enqueue_job`
(with the enqueuing trace and span), `started` as `perform` begins, then `retried` (with the error) when RQ will run it
again, or `finished`. A worker says it is there when it registers (`register_birth`), and every process that runs jobs
(the worker, each work horse) sends a `heartbeat` with them, so a job whose horse was killed leaves the console's
Live list. A queue with `is_async=False` sends none: its jobs run inside the caller.
"""

import functools
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from tracepanel.core import conf, errors, registry, services
from tracepanel.core.context import adopt, current_span, current_trace, now_us, record_span, recorded, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import Heartbeat, arguments, event_repr, task_event, task_repr, trigger
from tracepanel.storage import agent
from tracepanel.storage.transport import complete, get_writer, role

SYSTEM = "rq"
# job.meta keys tracepanel adds: they are saved with the job and read in the worker.
PARENT = "tracepanel_parent"
TRIGGER = "tracepanel_trigger"
MAX_RETRIES = "tracepanel_max_retries"
# The jobs this process performs now, for its heartbeat.
_running: set[str] = set()
_lock = threading.Lock()


def _running_ids() -> list[str]:
    with _lock:
        return sorted(_running)


_heart = Heartbeat(SYSTEM, _running_ids)
# True while a queue with is_async=False runs a job inside enqueue_job: that run is the caller's, never in flight.
_inline: ContextVar[bool] = ContextVar("tracepanel_rq_inline", default=False)


def _broker(connection: Any) -> Attrs:  # noqa: ANN401 (redis.Redis)
    kwargs = getattr(getattr(connection, "connection_pool", None), "connection_kwargs", None)
    if isinstance(kwargs, dict) and kwargs.get("host"):
        return services.for_broker(f"redis://{kwargs['host']}:{kwargs.get('port', 6379)}")
    return {"messaging.system": "redis"}


def _enqueue(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def enqueue_job(self: Any, job: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        trace = current_trace()
        start, mark = now_us(), recorded()
        span_id = new_id()
        job.meta[TRIGGER] = trigger(trace)
        if job.retries_left is not None and MAX_RETRIES not in job.meta:
            job.meta[MAX_RETRIES] = job.retries_left
        if trace is not None:
            job.meta[PARENT] = f"{trace.id}:{span_id}"
        inline = _inline.set(not getattr(self, "_is_async", True))
        try:
            result = original(self, job, *args, **kwargs)
        finally:
            _inline.reset(inline)
        if getattr(self, "_is_async", True):
            _enqueued(self.name, job, (trace.id, span_id) if trace is not None else None)
        if trace is not None:
            # RQ's Redis commands go under the enqueue span.
            adopt(span_id, mark)
            record_span(
                "task.enqueue",
                f"enqueue {job.func_name}",
                start=start,
                duration=now_us() - start,
                attrs={
                    "task.system": SYSTEM,
                    "task.name": job.func_name,
                    "task.id": job.id,
                    "task.queue": self.name,
                    "task.eager": not getattr(self, "_is_async", True),
                    "code.origin": origin(),
                    **_broker(self.connection),
                },
                span_id=span_id,
            )
        return result

    return enqueue_job


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


def _run_attrs(job: Any, *, eager: bool, started: int) -> Attrs:  # noqa: ANN401 (rq.job.Job)
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": str(job.func_name),
        "task.id": job.id,
        "task.queue": job.origin or None,
        "task.retries": _attempt(job),
        "task.eager": eager,
        "task.trigger": str(job.meta.get(TRIGGER) or ("request" if eager else "other")),
        **_broker(job.connection),
    }
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
    errors.capture(exc, handled=retried)
    trace.status = "ok" if retried else "error"


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
    """Wrap RQ's enqueue, perform and a worker's start; a no-op without RQ."""
    try:
        from rq.job import Job  # noqa: PLC0415 (optional dependency)
        from rq.queue import Queue  # noqa: PLC0415
        from rq.worker import BaseWorker  # noqa: PLC0415
    except ImportError:
        return
    if patched(Queue, "enqueue_job"):
        return
    patch(Queue, "enqueue_job", _enqueue(Queue.enqueue_job))
    patch(Job, "perform", _perform(Job.perform))
    patch(BaseWorker, "register_birth", _birth(BaseWorker.register_birth))
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
