"""Django Channels: each WebSocket event a consumer handles is a short `websocket` trace, each worker message a task.

A connection can stay open for hours, so it is not one trace: connect, each received frame, each message from the
channel layer and disconnect are traces of their own, linked to the connect trace (`connection`). Frames sent to the
client and channel layer sends are spans; a group message links the handlers it reaches to the span that sent it
(`sent_by`), like a task links to its enqueue span.

Background work in Channels is a message sent to a worker channel: `channel_layer.send("thumbnails", {"type":
"thumbnails.generate", ...})`, handled by the consumer that `manage.py runworker thumbnails` runs for that channel
(the ASGI application's ChannelNameRouter). Tracepanel counts these as tasks, `task.system = "channels"`: a send
to a normal channel name (no `!`) is a `task.enqueue` span, and the worker's handling of it a `task` trace, named
after the consumer's handler (`app.consumers.ThumbnailConsumer.thumbnails_generate`) and linked to the span that
sent it (`enqueued_by`). The rest is not: WebSocket and HTTP consumers serve requests, and the channels they get
messages on, a group's included, are process-specific (`!`), never a worker's.

A message's trace reaches the agent when it is handled. Until then task events say where it is, from each process
alike, whatever the layer: `enqueued` from the process that sent it (with the enqueuing trace and span), `started`
and `finished` from the worker. A worker says it is there as it starts and sends a `heartbeat` with the message it
handles, so a message whose worker was killed leaves the console's Live list, and says it stopped (`worker_stopped`)
as it exits. Channels never sends a message again: no `retried`. A message sent by a process without tracepanel has
no `enqueued`: its run gets an id of its own.
"""

import functools
import os
import socket
from contextvars import ContextVar
from typing import Any

from tracepanel.core import conf, errors, forks, profile, services
from tracepanel.core.context import current_trace, now_us, record_span, start_span, start_trace
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Link
from tracepanel.core.patching import patch, patched
from tracepanel.django.middleware import attrs_for_user
from tracepanel.integrations._tasks import Heartbeat, Workflow, arguments, sent_from, task_event, trigger
from tracepanel.storage import agent
from tracepanel.storage.transport import complete

SYSTEM = "channels"
PARENT = "tracepanel_parent"
# What a message sent to a worker channel carries to the worker: its task id, when it was sent, how and from which
# task's run (see _sent).
TASK = "tracepanel_task"
MARKER = "_tracepanel"
CONNECTION = "_tracepanel_connection"

_in_group_send: ContextVar[bool] = ContextVar("tracepanel_group_send", default=False)
# The messages this process's workers handle now, for its heartbeat.
_handling: set[str] = set()
_lock = forks.lock()


def _handling_ids() -> list[str]:
    with _lock:
        return sorted(_handling)


_heart = Heartbeat(SYSTEM, _handling_ids)

# websocket.* message types and the event names shown in the console.
EVENTS = {"websocket.connect": "connect", "websocket.receive": "receive", "websocket.disconnect": "disconnect"}


def _consumer_name(consumer: object) -> str:
    kind = type(consumer)
    return f"{kind.__module__}.{kind.__qualname__}"


def _size(message: dict[str, Any]) -> int | None:
    data = message.get("text") if message.get("text") is not None else message.get("bytes")
    return len(data) if isinstance(data, (str, bytes)) else None


def _attrs(consumer: Any, message: dict[str, Any], event: str) -> Attrs:  # noqa: ANN401 (any consumer)
    scope = consumer.scope
    attrs: Attrs = {
        "ws.event": event,
        "ws.path": scope.get("path"),
        "ws.channel": getattr(consumer, "channel_name", None),
        "code.view": _consumer_name(consumer),
        **attrs_for_user(scope.get("user")),
    }
    if event == "receive":
        attrs["ws.message_size"] = _size(message)
    if event == "disconnect":
        attrs["ws.close_code"] = message.get("code")
    if event not in EVENTS.values():
        attrs["ws.message_type"] = message["type"]
    return attrs


def _links(consumer: Any, message: dict[str, Any], event: str) -> list[Link]:  # noqa: ANN401
    links: list[Link] = []
    connection = getattr(consumer, CONNECTION, None)
    if event != "connect" and connection:
        links.append(Link(kind="connection", trace_id=connection[0], span_id=connection[1]))
    _link_parent(links, message, "sent_by")
    return links


def _wrap_dispatch(original: Any) -> Any:  # noqa: ANN401 (a coroutine function or Channels' sync-to-async wrapper)
    async def dispatch(self: Any, message: dict[str, Any]) -> None:  # noqa: ANN401
        scope = getattr(self, "scope", {})
        handler = original.__get__(self, type(self))
        if current_trace() is not None:
            return await handler(message)
        if scope.get("type") == "channel":
            return await _run(self, handler, message)
        if scope.get("type") != "websocket":
            return await handler(message)
        path = str(scope.get("path", ""))
        if conf.matches(path, conf.get_config().ignore_paths):
            return await handler(message)
        event = EVENTS.get(message["type"], message["type"])
        raised: BaseException | None = None
        attrs = _attrs(self, message, event)
        with start_trace("websocket", f"WS {event} {path}", attrs, root_kind="websocket") as trace:
            trace.links.extend(_links(self, message, event))
            if event == "connect":
                setattr(self, CONNECTION, (trace.id, trace.spans[0].id))
            try:
                await handler(message)
            except Exception as exc:  # noqa: BLE001 (re-raised below, after the trace is stored)
                raised = exc
                # StopConsumer is how a consumer ends normally (disconnect, close): not an error.
                if not isinstance(exc, _stop_consumer()):
                    errors.capture(exc, handled=False)
                    trace.status = trace.spans[0].status = "error"
        complete(trace)
        if raised is not None:
            raise raised
        return None

    return dispatch


def _stop_consumer() -> type[BaseException]:
    from channels.exceptions import StopConsumer  # noqa: PLC0415 (optional dependency)

    return StopConsumer


def _record_send(message: dict[str, Any]) -> None:
    trace = current_trace()
    if trace is None:
        return
    kind = message.get("type")
    if kind == "websocket.accept":
        trace.attrs["ws.accepted"] = True
    elif kind == "websocket.close":
        trace.attrs["ws.close_code"] = message.get("code")
    elif kind == "websocket.send":
        record_span("websocket.send", "send", start=now_us(), duration=0, attrs={"ws.message_size": _size(message)})


def _layer_attrs(layer: object) -> Attrs:
    """`messaging.system` and the server of a channel layer (Redis host from its config)."""
    name = type(layer).__name__
    if "Redis" in name:
        hosts: list[object] = list(getattr(layer, "hosts", None) or [])
        first = hosts[0] if hosts else None
        address = first.get("address") if isinstance(first, dict) else first
        # channels_redis keeps a ("host", port) entry as {"host": ..., "port": ...}.
        if isinstance(first, dict) and address is None and first.get("host"):
            address = f"redis://{first['host']}:{first.get('port', 6379)}"
        if isinstance(address, (list, tuple)):
            address = f"redis://{address[0]}:{address[1]}"
        return {**services.for_broker(str(address or "redis://localhost:6379")), "messaging.system": "redis"}
    return {"messaging.system": "memory" if "InMemory" in name else name.lower()}


def _instrument_layer(layer: Any) -> Any:  # noqa: ANN401 (any channel layer)
    if layer is None or getattr(layer, MARKER, False):
        return layer
    attrs = _layer_attrs(layer)
    group_send, send = layer.group_send, layer.send

    async def traced_group_send(group: str, message: dict[str, Any]) -> None:
        if current_trace() is None:
            return await group_send(group, message)
        span_attrs: Attrs = {**attrs, "channel.group": group, "channel.message_type": message.get("type")}
        with start_span("channel.send", f"group_send {group}", span_attrs) as span:
            trace = current_trace()
            if span is not None and trace is not None:
                message = {**message, PARENT: f"{trace.id}:{span.id}"}
            # Layers deliver a group message with send() per member: that is part of this span.
            token = _in_group_send.set(True)
            try:
                return await group_send(group, message)
            finally:
                _in_group_send.reset(token)

    async def traced_send(channel: str, message: dict[str, Any]) -> None:
        if _in_group_send.get():
            return await send(channel, message)
        if _is_worker_channel(channel):
            return await _enqueue(send, attrs, channel, message)
        if current_trace() is None:
            return await send(channel, message)
        span_attrs: Attrs = {**attrs, "channel.message_type": message.get("type")}
        with start_span("channel.send", f"send {message.get('type')}", span_attrs) as span:
            trace = current_trace()
            if span is not None and trace is not None:
                message = {**message, PARENT: f"{trace.id}:{span.id}"}
            return await send(channel, message)

    layer.group_send, layer.send = traced_group_send, traced_send
    setattr(layer, MARKER, True)
    return layer


# ---------- worker channels: tasks ----------


def _is_worker_channel(channel: str) -> bool:
    """A normal channel name: what runworker listens on. Consumers, and so groups, get process-specific ones (`!`)."""
    return "!" not in channel


def _handler_path(consumer: type, message_type: str) -> str:
    """`app.consumers.ThumbnailConsumer.thumbnails_generate`: the method Channels calls for the message's type."""
    return f"{consumer.__module__}.{consumer.__qualname__}.{message_type.replace('.', '_')}"


@functools.cache
def _consumer_of(channel: str) -> type | None:
    """The consumer the ASGI application runs for a worker channel (ChannelNameRouter), through any middleware."""
    from channels.routing import ChannelNameRouter, ProtocolTypeRouter, get_default_application  # noqa: PLC0415

    try:
        app: object = get_default_application()
    except Exception:  # noqa: BLE001 (no ASGI_APPLICATION, or it doesn't import: the message's type is all there is)
        return None
    # Routers and middleware nest a few levels deep; a bound keeps an app that wraps itself from looping.
    for _ in range(20):
        if isinstance(app, ProtocolTypeRouter):
            app = app.application_mapping.get("channel")
        elif isinstance(app, ChannelNameRouter):
            app = app.application_mapping.get(channel)
        elif isinstance(getattr(app, "consumer_class", None), type):
            return getattr(app, "consumer_class")  # noqa: B009 (as_asgi() sets it on a function)
        elif hasattr(app, "inner"):
            app = getattr(app, "inner")  # noqa: B009 (Channels' BaseMiddleware)
        else:
            return None
    return None


def _task_name(channel: str, message: dict[str, Any]) -> str:
    """The handler's path, as the worker names the run; the message's type when the routing doesn't say."""
    kind = str(message.get("type", ""))
    consumer = _consumer_of(channel)
    return _handler_path(consumer, kind) if consumer is not None else kind


def _fields(message: dict[str, Any]) -> dict[str, Any]:
    """The message's own fields: its arguments, without its type and what tracepanel added."""
    return {key: value for key, value in message.items() if key != "type" and not key.startswith("tracepanel_")}


def _event_arguments(message: dict[str, Any]) -> dict[str, object]:
    if not conf.get_config().capture_task_args:
        return {}
    return arguments((), _fields(message)).event()


async def _enqueue(send: Any, layer: Attrs, channel: str, message: dict[str, Any]) -> None:  # noqa: ANN401
    """A send to a worker channel: the message carries its task to the worker, a `task.enqueue` span inside a trace."""
    name = _task_name(channel, message)
    if conf.matches(name, conf.get_config().ignore_tasks):
        return await send(channel, message)
    trace = current_trace()
    task_id = new_id()
    workflow = sent_from(trace, task_id)
    task: dict[str, object] = {"id": task_id, "published": now_us(), "trigger": trigger(trace)}
    if workflow.parent_id:
        task.update(parent_id=workflow.parent_id, root_id=workflow.root_id)
    # Sent by a trace that asked to be profiled: the worker's run is profiled too.
    profile.carry(task, "profile")
    message = {**message, TASK: task}
    parent: dict[str, str] = {}
    if trace is None:
        await send(channel, message)
    else:
        # No code.origin, as for other sends: sync code sends through async_to_sync, whose coroutine runs on an event
        # loop, so the stack ends in the server's code, not in the line that sent it.
        attrs: Attrs = {"task.system": SYSTEM, "task.name": name, "task.id": task_id, "task.queue": channel, **layer}
        with start_span("task.enqueue", f"enqueue {name}", attrs) as span:
            if span is not None:
                parent = {"trace_id": trace.id, "span_id": span.id}
                message[PARENT] = f"{trace.id}:{span.id}"
            await send(channel, message)
    task_event("enqueued", SYSTEM, task_id, name=name, queue=channel, retries=0, **_event_arguments(message), **parent)
    return None


def _worker_name() -> str:
    return f"channels@{socket.gethostname()}:{os.getpid()}"


def _task_attrs(consumer: Any, message: dict[str, Any], name: str, task_id: str, task: dict[str, Any]) -> Attrs:  # noqa: ANN401
    attrs: Attrs = {
        "task.system": SYSTEM,
        "task.name": name,
        "task.id": task_id,
        "task.queue": consumer.scope.get("channel"),
        "task.worker": _heart.worker or None,
        "task.retries": 0,
        "task.eager": False,
        "task.trigger": str(task.get("trigger") or "other"),
        **_layer_attrs(consumer.channel_layer),
    }
    published = task.get("published")
    if isinstance(published, int):
        attrs["task.queue_wait"] = max(now_us() - published, 0)
    parent, root = task.get("parent_id"), task.get("root_id")
    attrs.update(Workflow(parent_id=parent, root_id=root).attrs(task_id))
    if conf.get_config().capture_task_args:
        attrs.update(arguments((), _fields(message)).attrs())
    return attrs


async def _run(consumer: Any, handler: Any, message: dict[str, Any]) -> None:  # noqa: ANN401
    """A message a worker handles: a `task` trace, between `started` and `finished`."""
    name = _handler_path(type(consumer), str(message.get("type", "")))
    if conf.matches(name, conf.get_config().ignore_tasks):
        return await handler(message)
    carried = message.get(TASK)
    task: dict[str, Any] = carried if isinstance(carried, dict) else {}
    task_id = str(task.get("id") or new_id())
    # A worker that runworker didn't start (one in a test) is still this process.
    _heart.start(_heart.worker or _worker_name())
    attrs = _task_attrs(consumer, message, name, task_id, task)
    raised: BaseException | None = None
    asked = profile.asked_by_parent(task.get("profile"))
    with start_trace("task", name, attrs, root_kind="task.run", profile=asked) as trace:
        _link_parent(trace.links, message, "enqueued_by")
        with _lock:
            _handling.add(task_id)
        task_event(
            "started",
            SYSTEM,
            task_id,
            name=name,
            queue=attrs["task.queue"],
            worker=_heart.worker,
            retries=0,
            **_event_arguments(message),
        )
        try:
            await handler(message)
        except Exception as exc:  # noqa: BLE001 (re-raised below, after the trace is stored)
            raised = exc
        # StopConsumer ends a consumer normally: not an error.
        failed = raised is not None and not isinstance(raised, _stop_consumer())
        if failed:
            errors.capture(raised, handled=False)
            trace.status = trace.spans[0].status = "error"
        trace.attrs["task.state"] = "FAILURE" if failed else "SUCCESS"
    complete(trace)
    with _lock:
        _handling.discard(task_id)
    task_event("finished", SYSTEM, task_id, worker=_heart.worker, state=trace.attrs["task.state"])
    if raised is not None:
        raise raised
    return None


def _link_parent(links: list[Link], message: dict[str, Any], kind: str) -> None:
    parent = message.get(PARENT)
    if isinstance(parent, str) and ":" in parent:
        trace_id, span_id = parent.split(":", 1)
        links.append(Link(kind=kind, trace_id=trace_id, span_id=span_id))


def _wrap_handle(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    async def handle(self: Any) -> None:  # noqa: ANN401 (channels.worker.Worker)
        # The worker is there before it handles anything: the console lists it among the workers.
        _heart.start(_worker_name())
        _heart.beat()
        return await original(self)

    return handle


def _wrap_run(original: Any) -> Any:  # noqa: ANN401
    @functools.wraps(original)
    def run(self: Any) -> Any:  # noqa: ANN401 (channels.worker.Worker)
        try:
            return original(self)
        finally:
            # The worker exits (asgiref's server ends at Ctrl+C): the console drops it at once.
            _heart.stopped()

    return run


def install() -> None:
    """Instrument Channels when it is installed; a no-op otherwise."""
    try:
        from channels import consumer, layers, worker  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    if patched(consumer.AsyncConsumer, "dispatch"):
        return
    # From the class __dict__: SyncConsumer.dispatch is a database_sync_to_async descriptor.
    patch(consumer.AsyncConsumer, "dispatch", _wrap_dispatch(consumer.AsyncConsumer.__dict__["dispatch"]))
    patch(consumer.SyncConsumer, "dispatch", _wrap_dispatch(consumer.SyncConsumer.__dict__["dispatch"]))

    async_send, sync_send = consumer.AsyncConsumer.send, consumer.SyncConsumer.send

    async def send(self: Any, message: dict[str, Any]) -> None:  # noqa: ANN401
        _record_send(message)
        await async_send(self, message)

    def send_sync(self: Any, message: dict[str, Any]) -> None:  # noqa: ANN401
        _record_send(message)
        sync_send(self, message)

    patch(consumer.AsyncConsumer, "send", send)
    patch(consumer.SyncConsumer, "send", send_sync)

    # The one place Channels builds a layer from CHANNEL_LAYERS.
    make_backend = getattr(layers.ChannelLayerManager, "_make_backend")  # noqa: B009 (private, untyped)

    def _make_backend(self: Any, name: str, config: Any) -> Any:  # noqa: ANN401
        return _instrument_layer(make_backend(self, name, config))

    patch(layers.ChannelLayerManager, "_make_backend", _make_backend)
    # Layers created before tracepanel was ready.
    for alias, layer in list(layers.channel_layers.backends.items()):
        layers.channel_layers.backends[alias] = _instrument_layer(layer)
    patch(worker.Worker, "handle", _wrap_handle(worker.Worker.handle))
    patch(worker.Worker, "run", _wrap_run(worker.Worker.run))
    # Its task events: the console's Live list of tasks (messages to worker channels).
    agent.sends("live_tasks")
