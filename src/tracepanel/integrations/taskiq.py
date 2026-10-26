"""Taskiq: every task a worker runs is a `task` trace, every `.kiq()` a `task.enqueue` span, and the two are linked.

Turned on by itself when Taskiq is installed: a middleware goes first on every broker, through Taskiq's own hooks. In
the process that kicks, `pre_send` puts the current trace and span in the message's labels and `post_send` records
the enqueue span; in the worker, `pre_execute` starts the run's trace, linked back to them (`enqueued_by`), with how
long it waited in the queue, and `post_execute` ends it. Being first, tracepanel's `on_error` and `post_execute` run
last, once the retry middlewares (SimpleRetryMiddleware, SmartRetryMiddleware) have decided between `retry` and
`failure`. Inside the run, SQL, HTTP calls, Redis commands, logs and the rest land in the task's trace and issues are
found like for requests. An async task is profiled as its asyncio Task, down to what it awaits; a sync one in the
worker thread Taskiq runs it in.

The InMemoryBroker runs a task in the process that kicks it, inside `kick()` with `await_inplace=True`: like an eager
Celery task, its trace links to the enqueue span in the caller's trace, and it is never in flight apart from it.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine.

A task's trace reaches the agent when it ends. Until then task events say where it is: `enqueued` after a kick (with
the enqueuing trace and span), `started` as it runs, `retried` (with the error) when a retry middleware sends it
again, then `finished`. A worker process (`taskiq worker`) says it is there as its broker starts and sends a
`heartbeat` with the tasks it runs, so a task whose worker was killed leaves the console's Live list; it says it
stopped (`worker_stopped`) as its broker shuts down. The InMemoryBroker sends none.

A task's trace says the worker process that ran it (`task.worker`), when a delayed task was due (`task.eta`, from its
`delay` label), its `priority` label and, on a run stopped by its `timeout` label, the limit (`task.timeout`). A task
kicked from inside another's run (any `.kiq()` in a task, a pipeline's next step) records that run as
`task.parent_id`, with the first task of the line as `task.root_id`. Tasks the scheduler kicks run with the trigger
`schedule`.
"""

import functools
import inspect
import os
import socket
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from tracepanel.core import conf, errors, forks, profile, registry, services
from tracepanel.core.context import adopt, current_trace, now_us, record_span, recorded, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import (
    Heartbeat,
    TaskRun,
    Workflow,
    arguments,
    event_repr,
    sent_from,
    task_event,
    task_repr,
    trigger,
)
from tracepanel.storage import agent
from tracepanel.storage.transport import complete, get_writer, role

SYSTEM = "taskiq"
# Labels tracepanel adds: they travel with the message to the worker (a retry sends them again).
PARENT = "tracepanel_parent"
PUBLISHED = "tracepanel_published"
TRIGGER = "tracepanel_trigger"
# Kicked by a trace that asked to be profiled: the run is profiled too.
PROFILE = "tracepanel_profile"
# The workflow a task belongs to: the task whose run kicked it, the first of that line.
PARENT_TASK = "tracepanel_parent_task"
ROOT_TASK = "tracepanel_root_task"
# When a delayed task is due: its `delay` label says how long from the kick.
ETA = "tracepanel_eta"
# What Taskiq adds to the name of each task's function.
ORIGINAL = "__taskiq_original"
# Broker classes -> messaging.system, for those with no server to name.
BROKERS = {"InMemoryBroker": "memory", "ZeroMQBroker": "zeromq"}


@dataclass(slots=True)
class _Run:
    """A task run in progress: its trace, how many times it ran before, and whether a retry middleware sent it again."""

    run: TaskRun
    task_id: str
    eager: bool
    retries: int
    token: "Token[_Run | None] | None" = None
    # The profile of the trace an inline run (await_inplace) runs inside: its sync task is sampled for it.
    inside: profile.Recording | None = None
    requeued: bool = False
    retried: bool = False


# The run open in this asyncio Task: Taskiq awaits a message's pre_execute, the task and its post_execute in one.
_run: ContextVar[_Run | None] = ContextVar("tracepanel_taskiq_run", default=None)
# Task id -> (when the kick started, the id the enqueue span will get, the trace's span count then).
_sending: dict[str, tuple[int, str, int]] = {}
_lock = forks.lock()
# The tasks this process runs now, for its heartbeat; one that was retried leaves at once.
_running: set[str] = set()


def _running_ids() -> list[str]:
    with _lock:
        return sorted(_running)


_heart = Heartbeat(SYSTEM, _running_ids)


def _worker() -> str:
    """This worker process, as its events and traces name it."""
    return f"taskiq@{socket.gethostname()}:{os.getpid()}"


def _name(broker: Any, task_name: str) -> str:  # noqa: ANN401 (taskiq.AsyncBroker)
    """`app.tasks.send_digest` for `app.tasks:send_digest`: the function's import path, as Celery names tasks."""
    task = broker.find_task(task_name)
    function = getattr(task, "original_func", None)
    module, name = getattr(function, "__module__", None), getattr(function, "__qualname__", None)
    if not (isinstance(module, str) and isinstance(name, str)):
        return task_name
    # Taskiq renames the function it wraps (for its process pool): its code keeps the name it was written with.
    return f"{module}.{name.removesuffix(ORIGINAL)}"


def _eager(broker: Any) -> bool:  # noqa: ANN401
    """The InMemoryBroker runs its tasks in the process that kicks them: no worker ever sees them."""
    from taskiq import InMemoryBroker  # noqa: PLC0415 (optional dependency)

    return isinstance(broker, InMemoryBroker)


def _queue(broker: Any) -> str | None:  # noqa: ANN401
    """The queue the broker sends to, when it has one (taskiq-redis's and taskiq-aio-pika's `queue_name`)."""
    queue = getattr(broker, "queue_name", None)
    return queue if isinstance(queue, str) and queue else None


def _broker(broker: Any) -> Attrs:  # noqa: ANN401
    """The broker's server, as for Celery: Redis from its connection pool, RabbitMQ and others from their URL."""
    pool = getattr(broker, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None)
    if isinstance(kwargs, dict) and kwargs.get("host"):
        return services.for_broker(f"redis://{kwargs['host']}:{kwargs.get('port', 6379)}")
    url = getattr(broker, "url", None)
    if isinstance(url, str) and url:
        return services.for_broker(url)
    system = next((BROKERS[kind.__name__] for kind in type(broker).__mro__ if kind.__name__ in BROKERS), None)
    return {"messaging.system": system} if system else {}


def _int(value: object) -> int | None:
    """A label's number: labels a message carries arrive as text unless the kick typed them."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError:
        return None


def _seconds(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _scheduling(labels: dict[str, Any]) -> Attrs:
    """When a delayed task was due and its priority, from its labels."""
    attrs: Attrs = {}
    due = labels.get(ETA)
    if isinstance(due, str) and due:
        attrs["task.eta"] = due
    priority = _int(labels.get("priority"))
    if priority is not None:
        attrs["task.priority"] = priority
    return attrs


def _arguments(message: Any) -> dict[str, object]:  # noqa: ANN401 (taskiq.TaskiqMessage)
    if not conf.get_config().capture_task_args:
        return {}
    return arguments(message.args, message.kwargs).event()


def _workflow(labels: dict[str, Any]) -> Workflow:
    parent, root = labels.get(PARENT_TASK), labels.get(ROOT_TASK)
    return Workflow(parent_id=str(parent) if parent else None, root_id=str(root) if root else None)


def _link(labels: dict[str, Any]) -> Link | None:
    parent = labels.get(PARENT)
    if isinstance(parent, str) and ":" in parent:
        parent_trace, parent_span = parent.split(":", 1)
        return Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span)
    return None


def _retry_of(message: Any) -> _Run | None:  # noqa: ANN401
    """The run this kick sends again: a retry middleware kicks the task with its own id from inside its run."""
    run = _run.get()
    return run if run is not None and run.task_id == message.task_id else None


class TracepanelHooks:
    """The hooks tracepanel needs; mixed into taskiq.TaskiqMiddleware when Taskiq is there (see install)."""

    broker: Any

    # ---------- the sending side ----------

    def pre_send(self, message: Any) -> Any:  # noqa: ANN401 (taskiq.TaskiqMessage)
        name = _name(self.broker, message.task_name)
        if conf.matches(name, conf.get_config().ignore_tasks):
            return message
        labels = message.labels
        retry = _retry_of(message)
        trace = current_trace()
        started = now_us()
        if retry is None:
            # A retry keeps the workflow it had, and how it was first triggered.
            workflow = sent_from(trace, message.task_id)
            if workflow.parent_id:
                labels[PARENT_TASK] = workflow.parent_id
                labels[ROOT_TASK] = workflow.root_id
            labels[TRIGGER] = "schedule" if labels.get("schedule_id") else trigger(trace)
        else:
            retry.requeued = True
        labels[PUBLISHED] = started
        labels.pop(ETA, None)
        delay = _seconds(labels.get("delay"))
        if delay:
            labels[ETA] = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        if trace is None:
            return message
        span_id = new_id()
        labels[PARENT] = f"{trace.id}:{span_id}"
        profile.carry(labels, PROFILE)
        with _lock:
            _sending[message.task_id] = (started, span_id, recorded())
        return message

    def post_send(self, message: Any) -> None:  # noqa: ANN401
        with _lock:
            sending = _sending.pop(message.task_id, None)
        name = _name(self.broker, message.task_name)
        if conf.matches(name, conf.get_config().ignore_tasks):
            return
        eager = _eager(self.broker)
        trace = current_trace()
        if not eager:
            parent = {"trace_id": trace.id, "span_id": sending[1]} if sending is not None and trace is not None else {}
            task_event(
                "enqueued",
                SYSTEM,
                message.task_id,
                name=name,
                queue=_queue(self.broker),
                retries=_int(message.labels.get("_retries")) or 0,
                **_arguments(message),
                **parent,
            )
        if sending is None or trace is None:
            return
        start, span_id, mark = sending
        # The broker's commands (Redis LPUSH…) go under the enqueue span; an inline run is a trace of its own.
        adopt(span_id, mark)
        record_span(
            "task.enqueue",
            f"enqueue {name}",
            start=start,
            duration=now_us() - start,
            attrs={
                "task.system": SYSTEM,
                "task.name": name,
                "task.id": message.task_id,
                "task.queue": _queue(self.broker),
                "task.eager": eager,
                "task.retries": _int(message.labels.get("_retries")) or 0,
                **_scheduling(message.labels),
                "code.origin": origin(),
                **_broker(self.broker),
            },
            span_id=span_id,
        )

    # ---------- the worker side ----------

    def startup(self) -> None:
        """A worker's broker starts: the console lists the process among the workers before it runs anything."""
        if getattr(self.broker, "is_worker_process", False):
            _heart.start(_worker())
            _heart.beat()

    def shutdown(self) -> None:
        """A worker's broker shuts down, its tasks done: what is queued is written, and the console drops it at once."""
        if getattr(self.broker, "is_worker_process", False):
            get_writer().flush()
            _heart.stopped()

    def pre_execute(self, message: Any) -> Any:  # noqa: ANN401
        name = _name(self.broker, message.task_name)
        if conf.matches(name, conf.get_config().ignore_tasks):
            return message
        labels = message.labels
        started = now_us()
        eager = _eager(self.broker)
        if not eager:
            _heart.start(_heart.worker or _worker())
        caller = current_trace()
        retries = _int(labels.get("_retries")) or 0
        attrs: Attrs = {
            "task.system": SYSTEM,
            "task.name": name,
            "task.id": message.task_id,
            "task.queue": _queue(self.broker),
            "task.worker": None if eager else _heart.worker,
            "task.retries": retries,
            "task.eager": eager,
            "task.trigger": str(labels.get(TRIGGER) or "other"),
            **_broker(self.broker),
            **_scheduling(labels),
        }
        attrs.update(_workflow(labels).attrs(message.task_id))
        published = _int(labels.get(PUBLISHED))
        if published is not None and not eager:
            attrs["task.queue_wait"] = max(started - published, 0)
        if eager and caller is not None:
            # Runs in the caller's process: web or command, not a worker.
            attrs["service.role"] = role(caller)
        if conf.get_config().capture_task_args:
            attrs.update(arguments(message.args, message.kwargs).attrs())
        asked = profile.asked_by_parent(labels.get(PROFILE))
        inside = profile.current.get() if eager and getattr(self.broker, "await_inplace", False) else None
        context = start_trace("task", name, attrs, root_kind="task.run", profile=asked)
        trace = context.__enter__()
        link = _link(labels)
        if link is not None:
            trace.links.append(link)
        run = _Run(
            run=TaskRun(context=context, trace=trace),
            task_id=message.task_id,
            eager=eager,
            retries=retries,
            inside=inside,
        )
        run.token = _run.set(run)
        if not eager:
            with _lock:
                _running.add(message.task_id)
            task_event(
                "started",
                SYSTEM,
                message.task_id,
                name=name,
                queue=_queue(self.broker),
                worker=_heart.worker,
                retries=retries,
                **_arguments(message),
            )
        return message

    def on_error(self, message: Any, result: Any, exception: BaseException) -> None:  # noqa: ANN401
        """The task raised. Tracepanel's hook runs after the retry middlewares': it knows whether it is retried."""
        run = _run.get()
        if run is None or run.task_id != message.task_id:
            return
        from taskiq.exceptions import NoResultError  # noqa: PLC0415 (optional dependency)

        if isinstance(exception, NoResultError):
            # How a task asks for its result not to be saved: no error.
            return
        trace = run.run.trace
        # Sent again (SimpleRetryMiddleware, SmartRetryMiddleware), or scheduled to be: its result is not saved then.
        run.retried = run.requeued or isinstance(result.error, NoResultError)
        if run.retried:
            trace.attrs["task.retry_reason"] = task_repr("reason", exception)
        timeout = _seconds(message.labels.get("timeout"))
        if isinstance(exception, TimeoutError) and timeout is not None:
            trace.attrs["task.timeout"] = timeout
        errors.capture(exception, handled=run.retried)
        trace.status = "ok" if run.retried else "error"
        if run.retried and not run.eager:
            with _lock:
                _running.discard(run.task_id)
            task_event(
                "retried",
                SYSTEM,
                run.task_id,
                worker=_heart.worker or None,
                retries=run.retries + 1,
                error=event_repr("reason", exception),
            )

    def post_execute(self, message: Any, result: Any) -> None:  # noqa: ANN401
        run = _run.get()
        if run is None or run.task_id != message.task_id:
            return
        from taskiq.exceptions import NoResultError  # noqa: PLC0415 (optional dependency)

        trace = run.run.trace
        error = result.error
        if run.retried:
            state = "RETRY"
        elif result.is_err and not isinstance(error, NoResultError):
            state = "FAILURE"
        else:
            state = "SUCCESS"
            if not result.is_err and conf.get_config().capture_task_args:
                trace.attrs["task.result"] = task_repr("result", result.return_value)
        trace.attrs["task.state"] = state
        run.run.context.__exit__(None, None, None)
        complete(trace)
        try:
            if run.token is not None:
                _run.reset(run.token)
        except ValueError:
            # Not the context it was set in: nothing left current all the same.
            _run.set(None)
        if run.eager or state == "RETRY":
            # A retry waits for its next attempt: it is still in flight.
            return
        with _lock:
            running = run.task_id in _running
            _running.discard(run.task_id)
        if running:
            task_event("finished", SYSTEM, run.task_id, worker=_heart.worker or None, state=state)


def _run_task(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    async def run_task(self: Any, target: Any, message: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        # A sync task runs in the receiver's thread pool: a profiled run follows it there, and so does the profile of
        # the trace an inline run runs inside. Not in a process pool, where the call is pickled.
        run = _run.get()
        recording = profile.current.get() or (run.inside if run is not None else None)
        if recording is not None and not self.is_process_pool and not inspect.iscoroutinefunction(target):
            target = _followed(target, recording)
        return await original(self, target, message, *args, **kwargs)

    return run_task


def _followed(target: Any, recording: profile.Recording) -> Any:  # noqa: ANN401 (a task's function)
    """The task's function, sampled for the profile in the thread that runs it; its signature for Taskiq's parsing."""

    @functools.wraps(target)
    def call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        with profile.SAMPLER.follow(recording):
            return target(*args, **kwargs)

    return call


def _add(broker: Any) -> None:  # noqa: ANN401
    if any(isinstance(middleware, TracepanelHooks) for middleware in broker.middlewares):
        return
    middleware = _middleware()
    middleware.set_broker(broker)
    # First: its pre_* hooks open the trace before the others run, its post_* and on_error hooks see their outcome.
    broker.middlewares.insert(0, middleware)


@functools.cache
def _middleware_class() -> type[Any]:
    from taskiq import TaskiqMiddleware  # noqa: PLC0415 (optional dependency)

    return type("TracepanelMiddleware", (TracepanelHooks, TaskiqMiddleware), {})


def _middleware() -> Any:  # noqa: ANN401 (a taskiq.TaskiqMiddleware)
    return _middleware_class()()


def install() -> None:
    """Add tracepanel to every Taskiq broker made from now on; a no-op without Taskiq."""
    try:
        from taskiq.abc.broker import AsyncBroker  # noqa: PLC0415 (optional dependency)
        from taskiq.receiver import Receiver  # noqa: PLC0415
    except ImportError:
        return
    if patched(AsyncBroker, "__init__"):
        return
    original = AsyncBroker.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        original(self, *args, **kwargs)
        _add(self)

    patch(AsyncBroker, "__init__", init)
    patch(Receiver, "run_task", _run_task(Receiver.run_task))
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
