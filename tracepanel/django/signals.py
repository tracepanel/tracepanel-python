"""Django signals: each send() / asend() with project receivers is a `signal` span, each receiver a span inside it.

So a slow post_save handler shows up where it ran, with its own queries under it.

pre_init / post_init are left out: they fire once per model instance and would drown the trace.
"""

import functools
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING

from django.db.models.signals import post_init, pre_init
from django.dispatch import Signal

from tracepanel.core.context import current_trace, start_span
from tracepanel.core.patching import patch, patched

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs

MARKER = "_tracepanel"

Receiver = Callable[..., object]

_sending: ContextVar[Signal | None] = ContextVar("tracepanel_signal", default=None)
_names: dict[int, str] = {}


def signal_name(signal: Signal) -> str:
    """`module.attribute` of the signal, found once by scanning loaded modules."""
    key = id(signal)
    if key not in _names:
        for module_name, module in list(sys.modules.items()):
            found = next((attr for attr, value in vars(module).items() if value is signal), None) if module else None
            if found:
                _names[key] = f"{module_name}.{found}"
                break
        else:
            _names[key] = "signal"
    return _names[key]


def qualname(receiver: object) -> str:
    target = getattr(receiver, "__func__", receiver)
    return f"{getattr(target, '__module__', '?')}.{getattr(target, '__qualname__', type(target).__qualname__)}"


def _project(receiver: object) -> bool:
    return not qualname(receiver).startswith("tracepanel.")


def _sender(sender: object) -> str:
    if sender is None:
        return "None"
    kind = sender if isinstance(sender, type) else type(sender)
    return f"{kind.__module__}.{kind.__qualname__}"


def _timed(receiver: Receiver, name: str) -> Receiver:
    attrs: Attrs = {"code.function": qualname(receiver), "signal.name": name}
    if iscoroutinefunction(receiver):

        @functools.wraps(receiver)
        async def acall(*args: object, **kwargs: object) -> object:
            with start_span("function", qualname(receiver), attrs):
                result: Awaitable[object] = receiver(*args, **kwargs)  # type: ignore[assignment]
                return await result

        setattr(acall, MARKER, receiver)
        return acall

    @functools.wraps(receiver)
    def call(*args: object, **kwargs: object) -> object:
        with start_span("function", qualname(receiver), attrs):
            return receiver(*args, **kwargs)

    setattr(call, MARKER, receiver)
    return call


def _original(receiver: object) -> object:
    return getattr(receiver, MARKER, receiver)


# Sequence, not list: covariant, so Django's own list[tuple[receiver, response]] fits.
Responses = Sequence[tuple[object, object]]

# Django's own receiver lookup, kept before it is patched.
_live_receivers = Signal._live_receivers  # noqa: SLF001 (the one place Django resolves receivers)


def _patched_live_receivers(self: Signal, sender: object) -> tuple[list[Receiver], list[Receiver]]:
    """While tracepanel times a send, the project's receivers come back wrapped in spans."""
    sync, asynchronous = _live_receivers(self, sender)
    if _sending.get() is not self:
        return sync, asynchronous
    name = signal_name(self)
    return (
        [_timed(receiver, name) if _project(receiver) else receiver for receiver in sync],
        [_timed(receiver, name) if _project(receiver) else receiver for receiver in asynchronous],
    )


def _span_attrs(signal: Signal, sender: object) -> "Attrs | None":
    """Attributes of the send's span, or None when there is nothing of the project's to time."""
    # Cheapest checks first: model signals fire for every object a query loads.
    if not signal.receivers or current_trace() is None:
        return None
    name = signal_name(signal)
    sync, asynchronous = _live_receivers(signal, sender)
    receivers = [receiver for receiver in [*sync, *asynchronous] if _project(receiver)]
    if not receivers:
        return None
    return {
        "signal.name": name,
        "signal.sender": _sender(sender),
        "signal.receivers": [qualname(receiver) for receiver in receivers],
    }


def _span_name(attrs: "Attrs") -> str:
    return f"signal {str(attrs['signal.name']).rsplit('.', 1)[-1]}"


def _unwrapped(responses: Responses) -> Responses:
    """Callers get their own receivers back, not the timing wrappers."""
    return [(_original(receiver), response) for receiver, response in responses]


def _wrap(send: Callable[..., Responses]) -> Callable[..., Responses]:
    @functools.wraps(send)
    def traced(self: Signal, sender: object, **named: object) -> Responses:
        # First and by identity: these two fire for every row a query loads.
        if self is pre_init or self is post_init:
            return send(self, sender, **named)
        attrs = _span_attrs(self, sender)
        if attrs is None:
            return send(self, sender, **named)
        token = _sending.set(self)
        try:
            with start_span("signal", _span_name(attrs), attrs):
                return _unwrapped(send(self, sender, **named))
        finally:
            _sending.reset(token)

    setattr(traced, MARKER, True)
    return traced


def _wrap_async(send: Callable[..., Awaitable[Responses]]) -> Callable[..., Awaitable[Responses]]:
    @functools.wraps(send)
    async def traced(self: Signal, sender: object, **named: object) -> Responses:
        attrs = _span_attrs(self, sender)
        if attrs is None:
            return await send(self, sender, **named)
        token = _sending.set(self)
        try:
            with start_span("signal", _span_name(attrs), attrs):
                return _unwrapped(await send(self, sender, **named))
        finally:
            _sending.reset(token)

    setattr(traced, MARKER, True)
    return traced


def install() -> None:
    if patched(Signal, "send"):
        return
    patch(Signal, "_live_receivers", _patched_live_receivers)
    patch(Signal, "send", _wrap(Signal.send))
    patch(Signal, "send_robust", _wrap(Signal.send_robust))
    patch(Signal, "asend", _wrap_async(Signal.asend))
    patch(Signal, "asend_robust", _wrap_async(Signal.asend_robust))
