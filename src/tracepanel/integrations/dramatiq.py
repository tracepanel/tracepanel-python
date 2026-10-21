"""Dramatiq: every message an actor processes is a `task` trace, every send a `task.enqueue` span, linked.

Turned on by itself when Dramatiq is installed: a middleware goes first on every broker. In the process that sends,
the message's options carry the current trace and span; in the worker, the message's run is a trace that links back
to them (`enqueued_by`) and knows how long it waited in the queue. Inside the run, SQL, logs and the rest land in
the task's trace and issues are found like for requests. Middleware runs its `after_*` hooks in reverse, so going
first means tracepanel sees the outcome last, once Retries has decided between `retry` and `failure`.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine.

A message's trace reaches the agent when it is processed. Until then task events say where it is: `enqueued` after a
send (with the enqueuing trace and span), `started` before it is processed, `finished` after (or skipped). When
Retries puts the message back, tracepanel sees the send in the thread still processing it and says `retried` (with the
error) right then, before another thread can start the next attempt. A delayed message moved to its queue when due is
not sent again. Each worker process says it is there when it boots and sends a `heartbeat` with the messages it
processes, so a message whose process was killed leaves the console's Live list.

A message's trace says the worker process that ran it (`task.worker`, the same name its events give), when a delayed
message was due (`task.eta`) and its actor's priority. A message sent from inside another's run (the next step of a
pipeline, a group's completion callback, an `on_success` or `on_failure` callback, any send in an actor) links to
that run and records it as `task.parent_id`, with the first message of the line as `task.root_id`; the messages of a
group share a `task.group_id`. A message stopped by its time limit (the TimeLimit middleware) fails with
TimeLimitExceeded and says the limit (`task.timeout`, in seconds).
"""

import functools
import os
import socket
import sys
from contextvars import ContextVar
from typing import Any

from tracepanel.core import conf, errors, forks, registry, services
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
    eta,
    event_repr,
    sent_from,
    task_event,
    task_repr,
    trigger,
)
from tracepanel.storage import agent
from tracepanel.storage.transport import complete, get_writer

SYSTEM = "dramatiq"
# Message options tracepanel adds: they travel with the message to the worker.
PARENT = "tracepanel_parent"
PUBLISHED = "tracepanel_published"
TRIGGER = "tracepanel_trigger"
# Set on a message's first send: a send of a message that has it is Retries' requeue or a delayed message moved on.
SENT = "tracepanel_sent"
# The workflow a message belongs to: the message whose run sent it, the first of that line, its group.
PARENT_TASK = "tracepanel_parent_task"
ROOT_TASK = "tracepanel_root_task"
GROUP = "tracepanel_group"
# When a delayed message is due: Dramatiq drops its own `eta` option as it moves the message to its queue.
ETA = "tracepanel_eta"
# The group whose messages this thread sends now (group.run).
_group: ContextVar[str | None] = ContextVar("tracepanel_dramatiq_group", default=None)
# The run open in this worker thread: before_process_message and after_process_message come from the same thread.
# Not keyed by message id: a retry keeps the id, and Retries requeues it (in its own after_ hook, before tracepanel's)
# soon enough for another thread to start the next attempt while this one is still being closed.
_run: ContextVar[TaskRun | None] = ContextVar("tracepanel_dramatiq_run", default=None)
# Message id -> (when the send started, the id the enqueue span will get, the trace's span count then).
_sending: dict[str, tuple[int, str, int]] = {}
_lock = forks.lock()
# Whether the send in progress in this thread is the message's first: before_enqueue tells after_enqueue.
_first_send: ContextVar[bool] = ContextVar("tracepanel_dramatiq_first_send", default=True)
# The messages this process processes now, for its heartbeat; one that was retried leaves at once.
_processing: set[str] = set()


def _processing_ids() -> list[str]:
    with _lock:
        return sorted(_processing)


_heart = Heartbeat(SYSTEM, _processing_ids)


def _queue(message: Any) -> str:  # noqa: ANN401 (dramatiq.Message)
    """The queue a message waits in, not its delay queue (`default.DQ`)."""
    from dramatiq.common import q_name  # noqa: PLC0415 (optional dependency)

    return q_name(message.queue_name)


def _arguments(message: Any) -> dict[str, object]:  # noqa: ANN401 (dramatiq.Message)
    if not conf.get_config().capture_task_args:
        return {}
    return arguments(message.args, message.kwargs).event()


def _actor_path(broker: Any, actor_name: str) -> str:  # noqa: ANN401 (dramatiq.Broker)
    """`app.tasks.send_digest` for actor `send_digest`: the function's import path, as Celery names tasks."""
    try:
        fn = broker.get_actor(actor_name).fn
    except Exception:  # noqa: BLE001 (an actor this process doesn't know: its name is all there is)
        return actor_name
    return f"{fn.__module__}.{fn.__qualname__}"


def _broker(broker: Any) -> Attrs:  # noqa: ANN401
    """The broker's server, as for Celery: Redis from its client, RabbitMQ from its connection parameters."""
    client = getattr(broker, "client", None)
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None)
    if isinstance(kwargs, dict) and kwargs.get("host"):
        return services.for_broker(f"redis://{kwargs['host']}:{kwargs.get('port', 6379)}")
    url = getattr(broker, "url", None)
    return services.for_broker(str(url)) if url else {}


class TracepanelHooks:
    """The hooks tracepanel needs; mixed into dramatiq.Middleware when Dramatiq is there (see install)."""

    # ---------- the sending side ----------

    def before_enqueue(self, broker: Any, message: Any, delay: int) -> None:  # noqa: ANN401, ARG002
        options = message.options
        first = SENT not in options
        _first_send.set(first)
        options[SENT] = True
        options[PUBLISHED] = now_us()
        trace = current_trace()
        if first:
            _join(options, sent_from(trace, message.message_id))
        if options.get("eta"):
            # A delayed send (a retry's backoff too): moved on when due, the message keeps it.
            options[ETA] = eta(options["eta"])
        if first or trace is not None:
            # A delayed message moved to its queue when due keeps how its send was triggered.
            options[TRIGGER] = trigger(trace)
        if trace is None:
            return
        span_id = new_id()
        options[PARENT] = f"{trace.id}:{span_id}"
        with _lock:
            _sending[message.message_id] = (now_us(), span_id, recorded())

    def after_enqueue(self, broker: Any, message: Any, delay: int) -> None:  # noqa: ANN401
        with _lock:
            sending = _sending.pop(message.message_id, None)
        self._sent(broker, message, sending)
        if sending is None or current_trace() is None:
            return
        start, span_id, mark = sending
        # The broker's commands (Redis) go under the enqueue span.
        adopt(span_id, mark)
        name = _actor_path(broker, message.actor_name)
        record_span(
            "task.enqueue",
            f"enqueue {name}",
            start=start,
            duration=now_us() - start,
            attrs={
                "task.system": SYSTEM,
                "task.name": name,
                "task.id": message.message_id,
                "task.queue": message.queue_name,
                "task.eta": message.options.get(ETA) if delay else None,
                "task.retries": int(message.options.get("retries") or 0),
                **_priority(broker, message),
                "code.origin": origin(),
                **_broker(broker),
            },
            span_id=span_id,
        )

    def _sent(self, broker: Any, message: Any, sending: tuple[int, str, int] | None) -> None:  # noqa: ANN401
        """The task events of a send: `enqueued` the first time, `enqueued` and `retried` for Retries' requeue."""
        name = _actor_path(broker, message.actor_name)
        run = _run.get()
        retry = run is not None and run.trace.attrs.get("task.id") == message.message_id
        if not (_first_send.get() or retry) or conf.matches(name, conf.get_config().ignore_tasks):
            return
        trace = current_trace()
        parent = {"trace_id": trace.id, "span_id": sending[1]} if sending is not None and trace is not None else {}
        retries = int(message.options.get("retries") or 0)
        task_event(
            "enqueued",
            SYSTEM,
            message.message_id,
            name=name,
            queue=_queue(message),
            retries=retries,
            **_arguments(message),
            **parent,
        )
        if retry:
            with _lock:
                _processing.discard(message.message_id)
            # Retries puts it back from its after_process_message, inside the worker's except block.
            error = sys.exc_info()[1]
            task_event(
                "retried",
                SYSTEM,
                message.message_id,
                worker=_heart.worker or None,
                retries=retries,
                error=event_repr("reason", error) if error is not None else None,
            )

    # ---------- the worker side ----------

    def after_worker_boot(self, broker: Any, worker: Any) -> None:  # noqa: ANN401, ARG002
        """The worker process is there before it processes anything: the console lists it among the workers."""
        _heart.start(f"dramatiq@{socket.gethostname()}:{os.getpid()}")
        _heart.beat()

    def before_process_message(self, broker: Any, message: Any) -> None:  # noqa: ANN401
        name = _actor_path(broker, message.actor_name)
        if conf.matches(name, conf.get_config().ignore_tasks):
            return
        options = message.options
        started = now_us()
        # A worker that didn't boot here (one in a test) is still this process.
        _heart.start(_heart.worker or f"dramatiq@{socket.gethostname()}:{os.getpid()}")
        attrs: Attrs = {
            "task.system": SYSTEM,
            "task.name": name,
            "task.id": message.message_id,
            "task.queue": message.queue_name,
            "task.worker": _heart.worker or None,
            "task.retries": int(options.get("retries") or 0),
            "task.eager": False,
            "task.trigger": str(options.get(TRIGGER) or "other"),
            **_broker(broker),
            **_priority(broker, message),
        }
        due = options.get(ETA)
        if due:
            attrs["task.eta"] = due
        attrs.update(_workflow(options).attrs(message.message_id))
        published = options.get(PUBLISHED)
        if isinstance(published, int):
            attrs["task.queue_wait"] = max(started - published, 0)
        if conf.get_config().capture_task_args:
            attrs.update(arguments(message.args, message.kwargs).attrs())
        context = start_trace("task", name, attrs, root_kind="task.run")
        trace = context.__enter__()
        parent = options.get(PARENT)
        if isinstance(parent, str) and ":" in parent:
            parent_trace, parent_span = parent.split(":", 1)
            trace.links.append(Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span))
        _run.set(TaskRun(context=context, trace=trace))
        self._started(message, name, attrs)

    def _started(self, message: Any, name: str, attrs: Attrs) -> None:  # noqa: ANN401
        with _lock:
            _processing.add(message.message_id)
        task_event(
            "started",
            SYSTEM,
            message.message_id,
            name=name,
            queue=_queue(message),
            worker=_heart.worker,
            retries=attrs["task.retries"],
            **_arguments(message),
        )

    def after_process_message(
        self,
        broker: Any,  # noqa: ANN401
        message: Any,  # noqa: ANN401
        *,
        result: object = None,
        exception: BaseException | None = None,
    ) -> None:
        run = _run.get()
        _run.set(None)
        if run is None:
            return
        trace = run.trace
        if exception is None:
            trace.attrs["task.state"] = "SUCCESS"
            if conf.get_config().capture_task_args:
                trace.attrs["task.result"] = task_repr("result", result)
        else:
            # Retries ran before this hook: a message it gave up on is failed, otherwise it went back to the queue.
            failed = bool(getattr(message, "failed", False))
            trace.attrs["task.state"] = "FAILURE" if failed else "RETRY"
            if not failed:
                trace.attrs["task.retry_reason"] = task_repr("reason", exception)
            if type(exception).__name__ == "TimeLimitExceeded":
                limit = _time_limit(broker, message)
                if limit is not None:
                    trace.attrs["task.timeout"] = limit
            errors.capture(exception, handled=not failed)
            trace.status = "error" if failed else "ok"
        run.context.__exit__(None, None, None)
        complete(trace)
        _finished(message, str(trace.attrs["task.state"]))

    def after_skip_message(self, broker: Any, message: Any) -> None:  # noqa: ANN401, ARG002
        run = _run.get()
        _run.set(None)
        if run is None:
            return
        run.trace.attrs["task.state"] = "SKIPPED"
        run.context.__exit__(None, None, None)
        complete(run.trace)
        _finished(message, "SKIPPED")

    def before_worker_shutdown(self, broker: Any, worker: Any) -> None:  # noqa: ANN401, ARG002
        """Write what is queued before the worker goes away."""
        get_writer().flush()


def _join(options: dict[str, Any], workflow: Workflow) -> None:
    """A message's first send: the run that sends it and that run's root, and the group being sent, if any."""
    if workflow.parent_id:
        options[PARENT_TASK] = workflow.parent_id
        options[ROOT_TASK] = workflow.root_id
    group = _group.get()
    if group:
        options[GROUP] = group


def _workflow(options: dict[str, Any]) -> Workflow:
    parent, root, group = options.get(PARENT_TASK), options.get(ROOT_TASK), options.get(GROUP)
    return Workflow(
        parent_id=str(parent) if parent else None,
        root_id=str(root) if root else None,
        group_id=str(group) if group else None,
    )


def _priority(broker: Any, message: Any) -> Attrs:  # noqa: ANN401
    """The actor's priority (Dramatiq's, for the worker's order of work): one this process doesn't know has none."""
    try:
        priority = broker.get_actor(message.actor_name).priority
    except Exception:  # noqa: BLE001
        return {}
    return {"task.priority": priority} if isinstance(priority, int) else {}


def _time_limit(broker: Any, message: Any) -> float | None:  # noqa: ANN401
    """The time limit that stopped the message, in seconds: its own, its actor's, or the TimeLimit middleware's."""
    from dramatiq.middleware import TimeLimit  # noqa: PLC0415 (optional dependency)

    default = next((m.time_limit for m in broker.middleware if isinstance(m, TimeLimit)), None)
    try:
        actor = broker.get_actor(message.actor_name).options.get("time_limit")
    except Exception:  # noqa: BLE001
        actor = None
    limit = message.options.get("time_limit") or actor or default
    return limit / 1000 if isinstance(limit, (int, float)) else None


def _group_run(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def run(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (dramatiq.group)
        # The messages sent meanwhile are this group's: an inner group is a group of its own.
        token = _group.set(new_id())
        try:
            return original(self, *args, **kwargs)
        finally:
            _group.reset(token)

    return run


def _finished(message: Any, state: str) -> None:  # noqa: ANN401
    """`finished`, unless the message was retried (it waits for its next attempt: Retries said so already).

    A retried attempt leaves the message alone: its next attempt may already run in another thread.
    """
    if state == "RETRY":
        return
    with _lock:
        processing = message.message_id in _processing
        _processing.discard(message.message_id)
    if processing:
        task_event("finished", SYSTEM, message.message_id, worker=_heart.worker or None, state=state)


def _add(broker: Any) -> None:  # noqa: ANN401
    if any(isinstance(middleware, TracepanelHooks) for middleware in broker.middleware):
        return
    # First: its before_* hooks open the trace before the others run, its after_* hooks see their outcome.
    broker.middleware.insert(0, _middleware())


def _middleware() -> Any:  # noqa: ANN401 (a dramatiq.Middleware)
    from dramatiq import Middleware  # noqa: PLC0415 (optional dependency)

    return type("TracepanelMiddleware", (TracepanelHooks, Middleware), {})()


def install() -> None:
    """Add tracepanel to every Dramatiq broker, present and future; a no-op without Dramatiq."""
    try:
        import dramatiq.broker  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    broker_class = dramatiq.broker.Broker
    if patched(broker_class, "__init__"):
        return
    original = broker_class.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        original(self, *args, **kwargs)
        _add(self)

    patch(broker_class, "__init__", init)
    from dramatiq.composition import group  # noqa: PLC0415

    patch(group, "run", _group_run(group.run))
    if dramatiq.broker.global_broker is not None:
        _add(dramatiq.broker.global_broker)
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
