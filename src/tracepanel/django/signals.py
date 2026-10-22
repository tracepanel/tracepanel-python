"""Django signals: each send with receivers is a `signal` span, each receiver a span inside it.

So a slow post_save handler shows up where it ran, with its own queries under it. Signal.send, send_robust, asend and
asend_robust are all covered, in requests, tasks and commands alike.

The send's span is named by the signal: Django's own by their name (`post_save`), any other by its dotted path
(`shop.signals.order_placed`). It carries the sender (`auth.Group` for a model, the dotted path of any other class),
the receivers, and the project's line that sent it (`code.origin`; `code.caller` names the library's line when no
project code did). Each receiver is a `function` span named by its dotted path, with its own file and line, and the
exception it raised: send_robust() swallows it, so the trace stays ok and the receiver's span says what failed.

A send nobody receives costs a counter, not a span: the trace's `signal.unreceived` lists each such signal and sender
once, with how many times it was sent (`pre_save auth.Group: 3`). Tracepanel's own receivers don't count, and a send
only they receive is left out.

Django's own lifecycle signals (request_started, request_finished, setting_changed, connection_created, pre_migrate,
post_migrate) are sent whatever the app does, and Django listens to most of them itself: reset_queries and
close_old_connections on every request, create_contenttypes and create_permissions after every migrate. They are
recorded only when a receiver of the app's listens, and then with Django's own beside it; sent to Django's own alone or
to nobody, they make no span and are not counted in `signal.unreceived`.

request_started is sent before the request's trace begins. When a receiver of the app's listens to it, what it does is
kept and becomes the start of the trace. request_finished is sent once the response has gone out, after the trace is
complete: it is not recorded.

`ignore_signals` leaves signals out entirely, by name or dotted path (glob patterns). By default these are pre_init
and post_init, which fire for every object a query loads, thousands in one request, so even counting them would cost
more than the rest of the signals together, and class_prepared, which fires for every model class built (every
historical model a migration renders). Their send is Django's own, not wrapped at all.
"""

import functools
import inspect
import sys
import types
import weakref
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from asgiref.sync import iscoroutinefunction
from django.core.signals import request_finished, request_started, setting_changed
from django.db.backends.signals import connection_created
from django.db.models.signals import post_migrate, pre_migrate
from django.dispatch import Signal
from django.dispatch.dispatcher import NO_RECEIVERS

from tracepanel.core import conf
from tracepanel.core.context import current_trace, holding, now_us, start_span
from tracepanel.core.frames import is_in_app, library_path, origin, relative
from tracepanel.core.model import Scalar, Trace
from tracepanel.core.patching import original, patch, patched

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs

MARKER = "_tracepanel"
UNRECEIVED = "signal.unreceived"
MAX_MESSAGE = 1000
SENDS = ("send", "send_robust", "asend", "asend_robust")

Receiver = Callable[..., object]
# Sequence, not list: covariant, so Django's own list[tuple[receiver, response]] fits.
Responses = Sequence[tuple[object, object]]

# The signal whose send tracepanel is timing: its receivers come back wrapped in spans.
_sending: ContextVar[Signal | None] = ContextVar("tracepanel_signal", default=None)
# What request_started did, kept until the request's trace begins.
_held: ContextVar[Trace | None] = ContextVar("tracepanel_request_started", default=None)

# Django's own lifecycle signals: recorded only when a receiver of the app's listens (see the module's docstring).
LIFECYCLE = frozenset(
    (request_started, request_finished, setting_changed, connection_created, pre_migrate, post_migrate)
)

# Django's own receiver lookup, kept before it is patched.
_live_receivers = Signal._live_receivers  # noqa: SLF001 (the one place Django resolves receivers)


@dataclass(frozen=True, slots=True)
class Named:
    """A signal as traces name it: `path` is where the project imports it from, `label` its span's name.

    `lifecycle` for Django's own lifecycle signals: recorded only when a receiver of the app's listens.
    """

    path: str
    label: str
    lifecycle: bool = False


# ---------- names ----------

# id() of each signal found in a loaded module -> its dotted path; the modules scanned so far.
_paths: dict[int, str] = {}
_scanned: set[str] = set()


def _defines(module_name: str) -> bool:
    """A module that is where signals live by Django's convention (signals.py), not one that imports them."""
    return module_name.rsplit(".", 1)[-1] == "signals"


def _scan() -> None:
    """Index the signals of every module loaded since the last scan: one pass, not one per signal."""
    for module_name, module in list(sys.modules.items()):
        if module_name in _scanned or not isinstance(module, types.ModuleType):
            continue
        _scanned.add(module_name)
        try:
            values = list(vars(module).items())
        except TypeError:
            continue
        for attr, value in values:
            # type(), not isinstance(): that would ask a lazy object (admin.site) its __class__, and set it up.
            if issubclass(type(value), Signal) and not attr.startswith("_"):
                known = _paths.get(id(value))
                # A receivers module imports the signal too: the signals module is where it lives.
                if known is None or (_defines(module_name) and not _defines(known.rsplit(".", 1)[0])):
                    _paths[id(value)] = f"{module_name}.{attr}"


def signal_name(signal: Signal) -> str:
    """`module.attribute` of the signal; "signal" for one no module holds (made inside a function)."""
    path = _paths.get(id(signal))
    if path is None:
        _scan()
        path = _paths.get(id(signal), "signal")
    return path


def _label(path: str) -> str:
    """Django's own signals by their name (post_save), any other by its dotted path."""
    return path.rsplit(".", 1)[-1] if path.startswith("django.") else path


def _ignored(named: Named, patterns: tuple[str, ...]) -> bool:
    return conf.matches(named.path, patterns) or conf.matches(named.label, patterns)


def _skip(signal: Signal) -> None:
    """Django's own send for an ignored signal: not even a wrapper that passes it on is left in the way."""
    for name in SENDS:
        patch(signal, name, types.MethodType(original(Signal, name), signal))


def _named(signal: Signal) -> Named | None:
    """How traces name the signal, worked out once and kept on it; None when `ignore_signals` leaves it out."""
    found = vars(signal).get(MARKER)
    if found is None:
        path = signal_name(signal)
        named = Named(path, _label(path), lifecycle=signal in LIFECYCLE)
        found = False if _ignored(named, conf.get_config().ignore_signals) else named
        setattr(signal, MARKER, found)
        if found is False:
            _skip(signal)
    return found or None


# ---------- senders and receivers ----------

_senders: dict[type, str] = {}


def _sender(sender: object) -> str | None:
    """A model by its label (auth.Group), any other class by its dotted path; an instance by its class."""
    if sender is None:
        return None
    kind = sender if isinstance(sender, type) else type(sender)
    name = _senders.get(kind)
    if name is None:
        meta = getattr(kind, "_meta", None)
        label = getattr(meta, "label", None)
        name = _senders[kind] = label if isinstance(label, str) else f"{kind.__module__}.{kind.__qualname__}"
    return name


def qualname(receiver: object) -> str:
    target = getattr(receiver, "__func__", receiver)
    return f"{getattr(target, '__module__', '?')}.{getattr(target, '__qualname__', type(target).__qualname__)}"


def _code(receiver: object) -> types.CodeType | None:
    """The code a receiver runs: a function's, a method's, a partial's, or a callable object's __call__."""
    target: Any = getattr(receiver, "__func__", receiver)
    while isinstance(target, functools.partial):
        target = target.func
    target = getattr(target, "__func__", target)
    if not callable(target):
        return None
    target = inspect.unwrap(target)
    code = getattr(target, "__code__", None)
    if code is None:
        # An object with a __call__ method: that method's code.
        code = getattr(type(target).__call__, "__code__", None)
    return code if isinstance(code, types.CodeType) else None


def _describe(receiver: object) -> "Attrs":
    attrs: Attrs = {"code.function": qualname(receiver)}
    code = _code(receiver)
    if code is not None:
        module = str(getattr(receiver, "__module__", "") or "")
        project = is_in_app(code.co_filename, module)
        attrs["code.filepath"] = relative(code.co_filename) if project else library_path(code.co_filename)
        attrs["code.lineno"] = code.co_firstlineno
    return attrs


# The function behind each receiver -> its attributes. Weak: a receiver Django holds weakly must still be able to go.
_receivers: "weakref.WeakKeyDictionary[object, Attrs]" = weakref.WeakKeyDictionary()


def _receiver_attrs(receiver: object) -> "Attrs":
    key = getattr(receiver, "__func__", receiver)
    try:
        found = _receivers.get(key)
    except TypeError:  # not weakly referenceable (a builtin): worked out each time
        return _describe(receiver)
    if found is None:
        found = _receivers[key] = _describe(receiver)
    return found


def _project(receiver: object) -> bool:
    """A receiver the app connected: Tracepanel's own are how it records, not what the app does."""
    return not str(getattr(getattr(receiver, "__func__", receiver), "__module__", "") or "").startswith("tracepanel.")


def _app_receiver(receiver: object) -> bool:
    """A receiver neither Django nor Tracepanel connected: the app's, or another library's."""
    module = str(getattr(getattr(receiver, "__func__", receiver), "__module__", "") or "")
    return not module.startswith(("django.", "tracepanel."))


def _failed(exc: Exception) -> "Attrs":
    kind = type(exc)
    message = str(exc)
    return {
        "exception.type": f"{kind.__module__}.{kind.__qualname__}",
        "exception.message": message if len(message) <= MAX_MESSAGE else f"{message[:MAX_MESSAGE]}…",
    }


def _timed(receiver: Receiver, named: Named) -> Receiver:
    attrs: Attrs = {**_receiver_attrs(receiver), "signal.name": named.path}
    name = str(attrs["code.function"])
    if iscoroutinefunction(receiver):

        @functools.wraps(receiver)
        async def acall(*args: object, **kwargs: object) -> object:
            with start_span("function", name, attrs) as span:
                try:
                    result: Awaitable[object] = receiver(*args, **kwargs)  # type: ignore[assignment]
                    return await result
                except Exception as exc:
                    if span is not None:
                        span.attrs.update(_failed(exc))
                    raise

        setattr(acall, MARKER, receiver)
        return acall

    @functools.wraps(receiver)
    def call(*args: object, **kwargs: object) -> object:
        with start_span("function", name, attrs) as span:
            try:
                return receiver(*args, **kwargs)
            except Exception as exc:
                if span is not None:
                    span.attrs.update(_failed(exc))
                raise

    setattr(call, MARKER, receiver)
    return call


def _original(receiver: object) -> object:
    return getattr(receiver, MARKER, receiver)


def _patched_live_receivers(self: Signal, sender: object) -> tuple[list[Receiver], list[Receiver]]:
    """While tracepanel times a send, the app's receivers come back wrapped in spans."""
    sync, asynchronous = _live_receivers(self, sender)
    if _sending.get() is not self:
        return sync, asynchronous
    named = vars(self).get(MARKER)
    if not isinstance(named, Named):
        return sync, asynchronous
    return (
        [_timed(receiver, named) if _project(receiver) else receiver for receiver in sync],
        [_timed(receiver, named) if _project(receiver) else receiver for receiver in asynchronous],
    )


# ---------- sends ----------


@dataclass(slots=True)
class Tally:
    """One signal and sender nobody received, in a trace: its place in `signal.unreceived`, and its sends."""

    index: int
    name: str
    sent: int = 0


def _count(trace: Trace, signal: Signal, named: Named, sender: object) -> None:
    """One more send nobody received: `signal.unreceived` lists each signal and sender once, with its count.

    Not Django's lifecycle signals: they are sent whatever the app does.
    """
    if named.lifecycle:
        return
    # By the sender's class, as it is named: an instance may not be hashable (a model object not saved yet).
    key = (signal, sender if isinstance(sender, type) else type(sender))
    tally = trace.counts.get(key)
    listed = trace.attrs.get(UNRECEIVED)
    if not isinstance(listed, list):
        listed = []
        trace.attrs[UNRECEIVED] = listed
    if not isinstance(tally, Tally):
        who = _sender(sender)
        tally = trace.counts[key] = Tally(len(listed), f"{named.label} {who}" if who else named.label)
        listed.append("")
    tally.sent += 1
    listed[tally.index] = f"{tally.name}: {tally.sent}"


# Where a send no project code made came from: the first frame past these (Django's admin saving an object).
_SEND_LAYERS = (
    "tracepanel.",
    "django.dispatch",
    "django.db.models",
    "django.db.transaction",
    "asgiref.",
    "contextlib",
    "functools",
    "asyncio.",
    "concurrent.",
    "threading",
)


def _caller() -> str | None:
    frame = sys._getframe(3)  # noqa: SLF001
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        if not module.startswith(_SEND_LAYERS):
            code = frame.f_code
            return f"{library_path(code.co_filename)}:{frame.f_lineno} in {code.co_qualname}"
        frame = frame.f_back
    return None


def _span_attrs(trace: Trace, signal: Signal, sender: object, *, robust: bool) -> "Attrs | None":
    """Attributes of the send's span, or None when it gets none (ignored, nobody receives it)."""
    named = _named(signal)
    if named is None:
        return None
    # Cheapest checks first: model signals fire for every object saved.
    if not signal.receivers or signal.sender_receivers_cache.get(sender) is NO_RECEIVERS:
        _count(trace, signal, named, sender)
        return None
    sync, asynchronous = _live_receivers(signal, sender)
    if not sync and not asynchronous:
        _count(trace, signal, named, sender)
        return None
    everyone = (*sync, *asynchronous)
    if named.lifecycle and not any(_app_receiver(receiver) for receiver in everyone):
        return None
    receivers: list[Scalar] = [qualname(receiver) for receiver in everyone if _project(receiver)]
    if not receivers:
        return None
    attrs: Attrs = {"signal.name": named.path}
    who = _sender(sender)
    if who is not None:
        attrs["signal.sender"] = who
    attrs["signal.receivers"] = receivers
    if robust:
        attrs["signal.robust"] = True
    sent_from = origin(3)
    attrs["code.origin"] = sent_from
    if sent_from is None:
        attrs["code.caller"] = _caller()
    return attrs


def _unwrapped(responses: Responses) -> Responses:
    """Callers get their own receivers back, not the timing wrappers."""
    return [(_original(receiver), response) for receiver, response in responses]


def _before_trace(signal: Signal, sender: object) -> bool:
    """request_started, sent outside any trace, to a receiver of the app's: what it does goes to the request's trace.

    Django's own receivers alone (reset_queries, close_old_connections) are left out: every request has them.
    """
    if signal is request_finished:
        _held.set(None)
        return False
    if signal is not request_started or not signal.receivers:
        return False
    sync, asynchronous = _live_receivers(signal, sender)
    return any(_app_receiver(receiver) for receiver in (*sync, *asynchronous))


def _wrap(send: Callable[..., Responses], *, robust: bool) -> Callable[..., Responses]:
    @functools.wraps(send)
    def traced(self: Signal, sender: object, **named: object) -> Responses:
        trace = current_trace()
        if trace is None:
            if (self is request_started or self is request_finished) and _before_trace(self, sender):
                return _hold(lambda: traced(self, sender, **named))
            return send(self, sender, **named)
        attrs = _span_attrs(trace, self, sender, robust=robust)
        if attrs is None:
            return send(self, sender, **named)
        token = _sending.set(self)
        try:
            with start_span("signal", vars(self)[MARKER].label, attrs):
                return _unwrapped(send(self, sender, **named))
        finally:
            _sending.reset(token)

    setattr(traced, MARKER, True)
    return traced


def _wrap_async(send: Callable[..., Awaitable[Responses]], *, robust: bool) -> Callable[..., Awaitable[Responses]]:
    @functools.wraps(send)
    async def traced(self: Signal, sender: object, **named: object) -> Responses:
        trace = current_trace()
        if trace is None:
            if (self is request_started or self is request_finished) and _before_trace(self, sender):
                held = Trace(id="", kind="held", name="", start=now_us())
                with holding(held):
                    responses = await traced(self, sender, **named)
                _held.set(held)
                return responses
            return await send(self, sender, **named)
        attrs = _span_attrs(trace, self, sender, robust=robust)
        if attrs is None:
            return await send(self, sender, **named)
        token = _sending.set(self)
        try:
            with start_span("signal", vars(self)[MARKER].label, attrs):
                return _unwrapped(await send(self, sender, **named))
        finally:
            _sending.reset(token)

    setattr(traced, MARKER, True)
    return traced


def _hold(run: Callable[[], Responses]) -> Responses:
    held = Trace(id="", kind="held", name="", start=now_us())
    with holding(held):
        responses = run()
    _held.set(held)
    return responses


def adopt_request_started(trace: Trace) -> int:
    """Put what request_started did at the start of the request's trace: how much earlier the trace now starts (µs).

    The middleware adds that to the trace's duration once it ends.
    """
    held = _held.get()
    if held is None:
        return 0
    _held.set(None)
    if not held.spans:
        return 0
    root = trace.spans[0]
    for span in held.spans:
        if span.parent_id is None:
            span.parent_id = root.id
    trace.spans[1:1] = held.spans
    trace.logs[:0] = held.logs
    earlier = max(trace.start - held.start, 0)
    trace.start = root.start = trace.start - earlier
    return earlier


def install() -> None:
    if patched(Signal, "send"):
        return
    patch(Signal, "_live_receivers", _patched_live_receivers)
    patch(Signal, "send", _wrap(Signal.send, robust=False))
    patch(Signal, "send_robust", _wrap(Signal.send_robust, robust=True))
    patch(Signal, "asend", _wrap_async(Signal.asend, robust=False))
    patch(Signal, "asend_robust", _wrap_async(Signal.asend_robust, robust=True))
    # Django's own signals are known now: the ignored ones (pre_init and post_init, sent for every object a query
    # loads, and class_prepared) keep Django's send, and never pass through here at all.
    from django.contrib.auth import signals as auth  # noqa: PLC0415
    from django.core import signals as core  # noqa: PLC0415
    from django.db.backends import signals as backends  # noqa: PLC0415
    from django.db.models import signals as models  # noqa: PLC0415

    for module in (models, core, auth, backends):
        for attr, value in vars(module).items():
            if isinstance(value, Signal):
                _paths[id(value)] = f"{module.__name__}.{attr}"
                _named(value)
