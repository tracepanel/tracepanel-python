"""Django Channels: every WebSocket event a consumer handles is a short `websocket` trace.

A connection can stay open for hours, so it is not one trace: connect, each received frame, each message from the
channel layer and disconnect are traces of their own, linked to the connect trace (`connection`). Frames sent to the
client and channel layer sends are spans; a group message links the handlers it reaches to the span that sent it
(`sent_by`), like a task links to its enqueue span.
"""

from contextvars import ContextVar
from typing import Any

from tracepanel.core import conf, errors, services
from tracepanel.core.context import current_trace, now_us, record_span, start_span, start_trace
from tracepanel.core.model import Attrs, Link
from tracepanel.core.patching import patch, patched
from tracepanel.django.middleware import attrs_for_user
from tracepanel.storage.transport import complete

SYSTEM = "channels"
PARENT = "tracepanel_parent"
MARKER = "_tracepanel"
CONNECTION = "_tracepanel_connection"

_in_group_send: ContextVar[bool] = ContextVar("tracepanel_group_send", default=False)

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
    parent = message.get(PARENT)
    if isinstance(parent, str) and ":" in parent:
        trace_id, span_id = parent.split(":", 1)
        links.append(Link(kind="sent_by", trace_id=trace_id, span_id=span_id))
    return links


def _wrap_dispatch(original: Any) -> Any:  # noqa: ANN401 (a coroutine function or Channels' sync-to-async wrapper)
    async def dispatch(self: Any, message: dict[str, Any]) -> None:  # noqa: ANN401
        scope = getattr(self, "scope", {})
        handler = original.__get__(self, type(self))
        if scope.get("type") != "websocket" or current_trace() is not None:
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
        if current_trace() is None or _in_group_send.get():
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


def install() -> None:
    """Instrument Channels when it is installed; a no-op otherwise."""
    try:
        from channels import consumer, layers  # noqa: PLC0415 (optional dependency)
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
