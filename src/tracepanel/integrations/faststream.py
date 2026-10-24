"""FastStream: each message a subscriber handles is a `task` trace, each publish a span of the trace that sends it.

Turned on by itself when FastStream is installed: a middleware goes first on every broker (Kafka, Confluent,
RabbitMQ, NATS, Redis), in an app of its own, under FastStream's FastAPI routers or in a `faststream-fastapi` app. A
message published inside a trace is a `task.enqueue` span there (`publish orders`), as Celery's, RQ's and Dramatiq's
are, with the broker in `messaging.system` and the topic, queue, subject or channel in `messaging.destination.name`.
Its headers carry the W3C `traceparent`, the trace and span that sent it and the id of its task in the console's Live,
as the Java SDK's messages do. The handler's run is a `task` trace named after its function, linked back to the
publish (`sent_by`). Inside it, SQL, HTTP calls, Redis commands, logs and the rest land in the trace as in any task's.
A message published from inside a handler's run follows it: that run's task is its `task.parent_id`, the first
task of the line its `task.root_id`, carried in its headers as the task's id is. A RabbitMQ message's priority is
`task.priority`, on the publish and on the run. No broker FastStream speaks to delays a message or limits a handler's
time, so there is no `task.eta` or `task.timeout`.

Arguments and results are kept for local debugging only (masked like locals) and never leave the machine. A message's
body is its argument.

A message's trace reaches the agent when its handler is done. Until then task events say where it is: `enqueued`
after a publish in a trace, `started` as its handler starts, `finished` after, or `retried` when the broker gets it
back to deliver again (a nack: Kafka seeks back, RabbitMQ and NATS JetStream redeliver). The app is a worker,
`<service>@<host>`: once a broker with subscribers starts, it sends a `heartbeat` with the messages it handles.
"""

import functools
import inspect
import socket
from collections import OrderedDict
from typing import Any

from tracepanel.core import conf, errors, forks, profile, registry, w3c
from tracepanel.core.context import current_trace, start_span, start_trace
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
)
from tracepanel.storage import agent
from tracepanel.storage.transport import complete

# FastStream's broker packages -> the system their messages belong to.
SYSTEMS = {"kafka": "kafka", "confluent": "kafka", "rabbit": "rabbitmq", "nats": "nats", "redis": "redis"}
# The headers a message carries, as the other SDKs name them: the trace and span that sent it (the link), and the id
# its task has in the console's Live from its publish on.
SENT_BY = "tracepanel-sent-by"
TASK_ID = "tracepanel-task-id"
# A message published from inside a handler's run: that run's task is its parent, the run's root its root.
PARENT_TASK = "tracepanel-parent-task"
ROOT_TASK = "tracepanel-root-task"
# Published by a trace that asked to be profiled: the handler's run is profiled too.
PROFILE = "tracepanel-profile"
# Messages whose last delivery was nacked, by system and task id, with how many times: a broker that doesn't count
# deliveries gets its count from here. The oldest go past a bound (a message nacked once may never come back).
FAILURES_KEPT = 1000
_failures: OrderedDict[tuple[str, str], int] = OrderedDict()
_lock = forks.lock()
# The messages this process handles now, by system, for its heartbeat.
_handling: dict[str, set[str]] = {}
_hearts: dict[str, Heartbeat] = {}


def _worker() -> str:
    """This app as a worker: `<service>@<host>`."""
    return f"{conf.get_config().service}@{socket.gethostname()}"


def _heart(system: str) -> Heartbeat:
    with _lock:
        heart = _hearts.get(system)
        if heart is None:
            heart = _hearts[system] = Heartbeat(system, functools.partial(_handling_ids, system))
        return heart


def _handling_ids(system: str) -> list[str]:
    with _lock:
        return sorted(_handling.get(system, ()))


def _system(broker: object) -> str:
    """The system of a broker's messages: `kafka` for KafkaBroker and Confluent's, `rabbitmq` for RabbitBroker."""
    parts = type(broker).__module__.split(".")
    package = parts[1] if len(parts) > 1 and parts[0] == "faststream" else type(broker).__name__.lower()
    return SYSTEMS.get(package, package)


def _first(raw: object) -> object:
    """A batch's first message: what its destination and position are read from."""
    if isinstance(raw, (list, tuple)) and raw:
        return raw[0]
    return raw


def _value(owner: object, name: str) -> object:
    """An attribute of a broker client's message, or what its method returns (Confluent's are methods)."""
    value = getattr(owner, name, None)
    return value() if callable(value) else value


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def _received(system: str, raw: object, subscriber: object) -> tuple[str, Attrs]:
    """Where a message was received from (topic, queue, subject, channel), and what its broker says of its place."""
    first = _first(raw)
    if system == "kafka":
        return _text(_value(first, "topic")), {
            "messaging.kafka.partition": _int(_value(first, "partition")),
            "messaging.kafka.offset": _int(_value(first, "offset")),
        }
    if system == "rabbitmq":
        queue = getattr(getattr(subscriber, "queue", None), "name", None)
        routing_key = _text(getattr(first, "routing_key", None))
        return _text(queue) or routing_key, {
            "messaging.rabbitmq.destination.routing_key": routing_key or None,
            "task.priority": _int(getattr(first, "priority", None)),
        }
    if system == "nats":
        return _text(getattr(first, "subject", None)), {}
    if system == "redis" and isinstance(first, dict):
        for key in ("channel", "list", "stream"):
            if first.get(key):
                return _text(first[key]), {}
    return "", {}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _delivered_before(system: str, raw: object, headers: dict[str, Any]) -> int | None:
    """How many times the broker delivered the message before, when it says; None when it doesn't count."""
    first = _first(raw)
    if system == "rabbitmq":
        # A quorum queue counts deliveries, a classic one says only whether the message was delivered before.
        count = headers.get("x-delivery-count")
        if isinstance(count, int):
            return count
        return 1 if getattr(first, "redelivered", False) else 0
    if system == "nats":
        delivered = _jetstream(first)
        return delivered - 1 if delivered is not None else None
    return None


def _jetstream(msg: object) -> int | None:
    """How many times JetStream delivered the message; None for a core NATS message (no redelivery)."""
    try:
        metadata = getattr(msg, "metadata", None)
    except Exception:  # noqa: BLE001 (nats-py raises when the message isn't JetStream's)
        return None
    return _int(getattr(metadata, "num_delivered", None))


def _redelivers(system: str, raw: object) -> bool:
    """Whether a nacked message comes back: Kafka seeks back to it, RabbitMQ and JetStream deliver it again."""
    if system in {"kafka", "rabbitmq"}:
        return True
    return system == "nats" and _jetstream(_first(raw)) is not None


def _handler_name(call_next: object) -> str | None:
    """`orders.handlers.on_order_created`: the function that handles the message, under the middlewares' calls.

    FastStream hands each middleware the next one's call, down to the handler's wrapper. Under FastAPI's router the
    function is wrapped for its dependencies, and unwrapped here. None for a call that is not a handler's (an RPC
    response).
    """
    call = call_next
    while isinstance(call, functools.partial):
        call = call.args[0] if call.args else None
    wrapper = getattr(call, "__self__", None)
    function = getattr(wrapper, "_declared_call", None) or getattr(wrapper, "_original_call", None)
    if function is None:
        return None
    function = inspect.unwrap(function)
    module = getattr(function, "__module__", None)
    name = getattr(function, "__qualname__", None) or getattr(function, "__name__", None)
    if not name:
        return None
    return f"{module}.{name}" if module else str(name)


def _workflow(headers: dict[str, Any]) -> Workflow:
    parent, root = _text(headers.get(PARENT_TASK)), _text(headers.get(ROOT_TASK))
    return Workflow(parent_id=parent or None, root_id=root or None)


def _publish_destination(system: str, cmd: object) -> tuple[str, Attrs]:
    """Where a message goes: the topic, subject or channel, or the exchange (the queue's name for the default one)."""
    destination = _text(getattr(cmd, "destination", None))
    if system == "rabbitmq":
        exchange = _text(getattr(getattr(cmd, "exchange", None), "name", None))
        options = getattr(cmd, "message_options", None)
        priority = options.get("priority") if isinstance(options, dict) else None
        return exchange or destination, {
            "messaging.rabbitmq.destination.routing_key": destination or None,
            "task.priority": _int(priority),
        }
    return destination, {}


def _known(attrs: Attrs) -> Attrs:
    """The attributes with a value: what a broker doesn't say is left out."""
    return {key: value for key, value in attrs.items() if value is not None}


def _event_arguments(body: object) -> dict[str, object]:
    if not conf.get_config().capture_task_args:
        return {}
    return arguments((body,), None).event()


def _count_failure(key: tuple[str, str], *, failed: bool) -> None:
    with _lock:
        if not failed:
            _failures.pop(key, None)
            return
        _failures[key] = _failures.pop(key, 0) + 1
        while len(_failures) > FAILURES_KEPT:
            _failures.popitem(last=False)


def _failed_before(key: tuple[str, str]) -> int:
    with _lock:
        return _failures.get(key, 0)


class TracepanelHooks:
    """The hooks tracepanel needs; mixed into FastStream's BaseMiddleware when FastStream is there (see install).

    One instance per message received, or per publish outside a handler. `system` is set by the factory.
    """

    system: str = ""
    context: Any
    _run: TaskRun | None = None
    _message: Any = None
    _task_id: str = ""
    _retries: int = 0
    _result: object = None

    # ---------- the sending side ----------

    async def publish_scope(self, call_next: Any, cmd: Any) -> Any:  # noqa: ANN401 (FastStream's types)
        trace = current_trace()
        headers = getattr(cmd, "headers", None)
        if trace is None or not isinstance(headers, dict):
            return await call_next(cmd)
        destination, extra = _publish_destination(self.system, cmd)
        bodies = tuple(getattr(cmd, "batch_bodies", ()) or ())
        # A batch's messages are each a task of their own: none of them is this publish's.
        task_id = new_id() if len(bodies) <= 1 else None
        attrs = _known(
            {
                "task.system": self.system,
                "task.name": destination,
                "task.id": task_id,
                "task.queue": destination,
                "messaging.system": self.system,
                "messaging.operation": "publish",
                "messaging.destination.name": destination,
                **extra,
                "messaging.batch.message_count": len(bodies) if len(bodies) > 1 else None,
                "code.origin": origin(),
            }
        )
        # An enqueue like Celery's, RQ's and Dramatiq's: the console and the store see the queues alike.
        with start_span("task.enqueue", f"publish {destination}", attrs) as span:
            if span is None:
                return await call_next(cmd)
            span.attrs["w3c.span_id"] = w3c.span_id(span.id)
            # The app's own traceparent stays: it propagates its trace context itself.
            headers.setdefault(w3c.HEADER, w3c.outgoing(trace, span.id))
            headers[SENT_BY] = f"{trace.id}:{span.id}"
            profile.carry(headers, PROFILE)
            if task_id is not None:
                headers[TASK_ID] = task_id
            workflow = sent_from(trace, task_id or "")
            if workflow.parent_id and workflow.root_id:
                headers[PARENT_TASK] = workflow.parent_id
                headers[ROOT_TASK] = workflow.root_id
            try:
                result = await call_next(cmd)
            except Exception as exc:
                span.attrs["error.type"] = type(exc).__name__
                raise
        if task_id is not None:
            # Named after where it goes until a handler takes it: the handler's function names it then.
            task_event(
                "enqueued",
                self.system,
                task_id,
                name=destination,
                queue=destination,
                trace_id=trace.id,
                span_id=span.id,
                **_event_arguments(bodies[0] if bodies else None),
            )
        return result

    # ---------- the receiving side ----------

    async def consume_scope(self, call_next: Any, msg: Any) -> Any:  # noqa: ANN401
        source = getattr(msg, "source_type", None)
        name = _handler_name(call_next) if self._run is None else None
        if name is None or getattr(source, "value", source) not in {None, "CONSUME"}:
            return await call_next(msg)
        if conf.matches(name, conf.get_config().ignore_tasks):
            return await call_next(msg)
        await self._begin(msg, name)
        result = await call_next(msg)
        self._result = _returned(result)
        return result

    async def _begin(self, msg: Any, name: str) -> None:  # noqa: ANN401
        system = self.system
        raw = msg.raw_message
        subscriber = self.context.get_local("handler_")
        destination, place = _received(system, raw, subscriber)
        headers: dict[str, Any] = msg.headers if isinstance(msg.headers, dict) else {}
        task_id = _text(headers.get(TASK_ID)) or _text(msg.message_id) or new_id()
        said = _delivered_before(system, raw, headers)
        retries = max(said or 0, _failed_before((system, task_id)))
        sender = _text(headers.get(SENT_BY)).split(":")
        linked = len(sender) == 2 and all(sender)  # noqa: PLR2004 (trace and span)
        caller = w3c.incoming(_text(headers.get(w3c.HEADER)) or None)
        worker = _worker()
        attrs: Attrs = {
            "task.system": system,
            "task.name": name,
            "task.id": task_id,
            "task.queue": destination,
            "task.worker": worker,
            "task.retries": retries,
            "task.eager": False,
            "task.trigger": "message" if linked or "w3c.parent_span_id" in caller else "other",
            "messaging.system": system,
            "messaging.operation": "process",
            "messaging.destination.name": destination,
            "messaging.message.id": _text(msg.message_id) or None,
            **place,
            **_workflow(headers).attrs(task_id),
        }
        # Sent by a trace the agent has: linked to it. The caller's span would link it a second time.
        if "w3c.trace_id" in caller:
            attrs["w3c.trace_id"] = caller["w3c.trace_id"]
        if not linked:
            attrs.update(caller)
        shown: dict[str, object] = {}
        if conf.get_config().capture_task_args:
            body = await _decoded(msg)
            called = arguments((body,), None)
            attrs.update(called.attrs())
            shown = called.event()
        asked = profile.asked_by_parent(_text(headers.get(PROFILE)))
        context = start_trace("task", name, _known(attrs), root_kind="task.run", profile=asked)
        trace = context.__enter__()
        if linked:
            trace.links.append(Link(kind="sent_by", trace_id=sender[0], span_id=sender[1]))
        self._run = TaskRun(context=context, trace=trace)
        self._message = msg
        self._task_id = task_id
        self._retries = retries
        heart = _heart(system)
        heart.start(worker)
        with _lock:
            _handling.setdefault(system, set()).add(task_id)
        task_event(
            "started",
            system,
            task_id,
            name=name,
            queue=destination,
            worker=worker,
            retries=retries,
            **shown,
        )

    async def after_processed(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: object = None,
    ) -> bool | None:
        del exc_type, exc_tb
        run, self._run = self._run, None
        if run is None:
            return False
        trace = run.trace
        state = self._outcome(exc_val)
        trace.attrs["task.state"] = state
        if state == "SUCCESS":
            if exc_val is None and conf.get_config().capture_task_args:
                trace.attrs["task.result"] = task_repr("result", self._result)
        else:
            # Nacked or rejected on purpose (NackMessage, RejectMessage, or by hand) is no error to show.
            if exc_val is not None and not _control(exc_val):
                errors.capture(exc_val, handled=state == "RETRY")
            # A message to be delivered again is handled: its next attempt is a trace of its own.
            trace.status = "error" if state == "FAILURE" else "ok"
        if state == "RETRY":
            trace.attrs["task.retry_reason"] = task_repr("reason", exc_val) if exc_val is not None else "nack"
        run.context.__exit__(None, None, None)
        complete(trace)
        self._finished(state, exc_val)
        return False

    def _outcome(self, exc: BaseException | None) -> str:
        """SUCCESS, RETRY (the broker delivers it again) or FAILURE, as FastStream acknowledges the message after."""
        msg = self._message
        committed = getattr(getattr(msg, "committed", None), "value", None)
        kind = type(exc).__name__ if _control(exc) else None
        if kind == "AckMessage" or (exc is None and committed in {None, "ACKED"}):
            return "SUCCESS"
        nacked = kind == "NackMessage" or committed == "NACKED"
        if exc is not None and kind is None and committed is None:
            policy = getattr(self.context.get_local("handler_"), "ack_policy", None)
            nacked = getattr(policy, "value", policy) == "nack_on_error"
        if nacked and _redelivers(self.system, getattr(msg, "raw_message", None)):
            return "RETRY"
        return "FAILURE"

    def _finished(self, state: str, exc: BaseException | None) -> None:
        system, task_id = self.system, self._task_id
        _count_failure((system, task_id), failed=state == "RETRY")
        with _lock:
            _handling.get(system, set()).discard(task_id)
        if state == "RETRY":
            task_event(
                "retried",
                system,
                task_id,
                worker=_worker(),
                # This attempt counted: the next one's.
                retries=self._retries + 1,
                error=event_repr("reason", exc) if exc is not None and not _control(exc) else None,
            )
            return
        task_event("finished", system, task_id, worker=_worker(), state=state)


def _control(exc: BaseException | None) -> bool:
    """FastStream's AckMessage, NackMessage or RejectMessage: a handler's answer to the broker, not an error."""
    from faststream.exceptions import HandlerException  # noqa: PLC0415 (optional dependency)

    return isinstance(exc, HandlerException)


def _returned(result: object) -> object:
    """What the handler returned: a FastAPI handler's comes wrapped in FastStream's Response, with headers."""
    from faststream.response import Response  # noqa: PLC0415 (optional dependency)

    return result.body if isinstance(result, Response) else result


async def _decoded(msg: Any) -> object:  # noqa: ANN401 (a StreamMessage)
    """The message's body as its handler gets it (JSON decoded), or its bytes when it doesn't decode."""
    try:
        return await msg.decode()
    except Exception:  # noqa: BLE001 (the handler meets the same error, and its trace shows it)
        return getattr(msg, "body", None)


class _Factory:
    """What FastStream calls a broker middleware: a factory that makes the middleware for each message."""

    def __init__(self, system: str, middleware: type[Any]) -> None:
        self.system = system
        self.middleware = middleware

    def __call__(self, msg: object, /, *, context: object) -> Any:  # noqa: ANN401 (a BaseMiddleware)
        made = self.middleware(msg, context=context)
        made.system = self.system
        return made


@functools.cache
def _middleware() -> type[Any]:
    from faststream import BaseMiddleware  # noqa: PLC0415 (optional dependency)

    return type("TracepanelMiddleware", (TracepanelHooks, BaseMiddleware), {})


def _add(broker: Any) -> None:  # noqa: ANN401 (a FastStream broker)
    if any(isinstance(middleware, _Factory) for middleware in broker.middlewares):
        return
    # First: its spans and traces hold what the other middlewares do (OpenTelemetry's spans among them).
    broker.insert_middleware(_Factory(_system(broker), _middleware()))


def install() -> None:
    """Add tracepanel to every FastStream broker made from now on; a no-op without FastStream."""
    try:
        from faststream._internal.broker import BrokerUsecase  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    if patched(BrokerUsecase, "__init__"):
        return
    original_init = BrokerUsecase.__init__
    original_start = BrokerUsecase.start

    def init(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        original_init(self, *args, **kwargs)
        _add(self)

    async def start(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        await original_start(self, *args, **kwargs)
        if self.subscribers:
            # The app is there before it handles anything: the console lists it among the workers.
            heart = _heart(_system(self))
            heart.start(_worker())
            heart.beat()

    patch(BrokerUsecase, "__init__", init)
    patch(BrokerUsecase, "start", start)
    # Its task events: the console's Live list of tasks.
    agent.sends("live_tasks")
    # Records of handler code, through the registry: off with logging_enabled.
    registry.get("logging").install()
