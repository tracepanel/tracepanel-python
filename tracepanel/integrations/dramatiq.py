"""Dramatiq: every message an actor processes is a `task` trace, every send a `task.enqueue` span, linked.

Turned on by itself when Dramatiq is installed: a middleware goes first on every broker. In the process that sends,
the message's options carry the current trace and span; in the worker, the message's run is a trace that links back
to them (`enqueued_by`) and knows how long it waited in the queue. Inside the run, SQL, logs and the rest land in
the task's trace and issues are found like for requests. Middleware runs its `after_*` hooks in reverse, so going
first means tracepanel sees the outcome last, once Retries has decided between `retry` and `failure`.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine.
"""

import threading
from contextvars import ContextVar
from typing import Any

from tracepanel.core import conf, errors, registry, services
from tracepanel.core.context import current_trace, now_us, record_span, start_trace
from tracepanel.core.frames import origin
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._tasks import TaskRun, task_repr, trigger
from tracepanel.storage.transport import complete, get_writer

SYSTEM = "dramatiq"
# Message options tracepanel adds: they travel with the message to the worker.
PARENT = "tracepanel_parent"
PUBLISHED = "tracepanel_published"
TRIGGER = "tracepanel_trigger"
# The run open in this worker thread: before_process_message and after_process_message come from the same thread.
# Not keyed by message id: a retry keeps the id, and Retries requeues it (in its own after_ hook, before tracepanel's)
# soon enough for another thread to start the next attempt while this one is still being closed.
_run: ContextVar[TaskRun | None] = ContextVar("tracepanel_dramatiq_run", default=None)
# Message id -> (when the send started, the id the enqueue span will get).
_sending: dict[str, tuple[int, str]] = {}
_lock = threading.Lock()


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
        options[PUBLISHED] = now_us()
        options[TRIGGER] = trigger(current_trace())
        trace = current_trace()
        if trace is None:
            return
        span_id = new_id()
        options[PARENT] = f"{trace.id}:{span_id}"
        with _lock:
            _sending[message.message_id] = (now_us(), span_id)

    def after_enqueue(self, broker: Any, message: Any, delay: int) -> None:  # noqa: ANN401
        with _lock:
            sending = _sending.pop(message.message_id, None)
        if sending is None or current_trace() is None:
            return
        start, span_id = sending
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
                "task.eta": f"+{delay} ms" if delay else None,
                "task.retries": int(message.options.get("retries") or 0),
                "code.origin": origin(),
                **_broker(broker),
            },
            span_id=span_id,
        )

    # ---------- the worker side ----------

    def before_process_message(self, broker: Any, message: Any) -> None:  # noqa: ANN401
        name = _actor_path(broker, message.actor_name)
        if conf.matches(name, conf.get_config().ignore_tasks):
            return
        options = message.options
        started = now_us()
        attrs: Attrs = {
            "task.system": SYSTEM,
            "task.name": name,
            "task.id": message.message_id,
            "task.queue": message.queue_name,
            "task.retries": int(options.get("retries") or 0),
            "task.eager": False,
            "task.trigger": str(options.get(TRIGGER) or "other"),
            **_broker(broker),
        }
        published = options.get(PUBLISHED)
        if isinstance(published, int):
            attrs["task.queue_wait"] = max(started - published, 0)
        if conf.get_config().capture_task_args:
            attrs["task.args"] = task_repr("args", tuple(message.args))
            attrs["task.kwargs"] = task_repr("kwargs", dict(message.kwargs))
        context = start_trace("task", name, attrs, root_kind="task.run")
        trace = context.__enter__()
        parent = options.get(PARENT)
        if isinstance(parent, str) and ":" in parent:
            parent_trace, parent_span = parent.split(":", 1)
            trace.links.append(Link(kind="enqueued_by", trace_id=parent_trace, span_id=parent_span))
        _run.set(TaskRun(context=context, trace=trace))

    def after_process_message(
        self,
        broker: Any,  # noqa: ANN401, ARG002
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
            errors.capture(exception, handled=not failed)
            trace.status = "error" if failed else "ok"
        run.context.__exit__(None, None, None)
        complete(trace)

    def after_skip_message(self, broker: Any, message: Any) -> None:  # noqa: ANN401, ARG002
        run = _run.get()
        _run.set(None)
        if run is None:
            return
        run.trace.attrs["task.state"] = "SKIPPED"
        run.context.__exit__(None, None, None)
        complete(run.trace)

    def before_worker_shutdown(self, broker: Any, worker: Any) -> None:  # noqa: ANN401, ARG002
        """Write what is queued before the worker goes away."""
        get_writer().flush()


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
    if dramatiq.broker.global_broker is not None:
        _add(dramatiq.broker.global_broker)
    # Records of task code, through the registry: off with logging_enabled.
    registry.get("logging").install()
