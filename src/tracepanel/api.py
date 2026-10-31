"""Instrument your own code: spans inside the current trace, traces where there is none, attributes, events.

    with tracepanel.span("pricing.calculate", cart_id=cart.id) as span:
        span.set("total", total)

    @tracepanel.traced()
    def rebuild_index(): ...

    with tracepanel.trace("sync-prices"):        # a script, a consumer, a cron job outside Celery
        sync_prices()

Everything is a no-op outside a trace and outside DEBUG, so calls can stay in production code. Keyword attributes
land under `app.` (`cart_id` → `app.cart_id`); pass dotted names through `attrs={...}` to choose the namespace.
"""

import contextlib
import functools
import inspect
from collections.abc import Awaitable, Callable, Generator, Iterator, Mapping
from typing import Any, ParamSpec, TypeVar, overload

from tracepanel.core import conf
from tracepanel.core.context import (
    current_span,
    current_trace,
    now_us,
    profile_current,
    record_span,
    start_span,
    start_trace,
)
from tracepanel.core.frames import safe_repr
from tracepanel.core.model import Attrs, AttrValue, Span, Trace
from tracepanel.core.profile import MEMORY, TIME
from tracepanel.storage.transport import complete

P = ParamSpec("P")
R = TypeVar("R")

APP = "app"
SCALARS = (str, int, float, bool)


def _key(name: str) -> str:
    return name if "." in name else f"{APP}.{name}"


def _value(key: str, value: object) -> AttrValue:
    if value is None or isinstance(value, SCALARS):
        return value
    if isinstance(value, (list, tuple)) and all(item is None or isinstance(item, SCALARS) for item in value):
        return list(value)
    return safe_repr(key, value)


def _attrs(attrs: Mapping[str, object] | None, extra: Mapping[str, object]) -> Attrs:
    merged = {**(attrs or {}), **extra}
    return {_key(key): _value(key, value) for key, value in merged.items()}


def _enabled() -> bool:
    """Whether tracepanel records: the framework integration decides (Django: DEBUG)."""
    return conf.get_config().enabled


class SpanHandle:
    """What `span()` and `trace()` yield: set attributes while the block runs (a no-op outside a trace).

    For `span()` they go on the span; for a `trace()` of its own, on the trace, where search can find them.
    """

    __slots__ = ("_target",)

    def __init__(self, target: Span | Trace | None) -> None:
        self._target = target

    @property
    def recording(self) -> bool:
        return self._target is not None

    def set(self, key: str, value: object) -> None:
        if self._target is not None:
            self._target.attrs[_key(key)] = _value(key, value)


@contextlib.contextmanager
def span(name: str, *, attrs: Mapping[str, object] | None = None, **values: object) -> Iterator[SpanHandle]:
    """Time a block as a `function` span of the current trace; SQL, logs and errors inside attach to it."""
    with start_span("function", name, _attrs(attrs, values)) as current:
        yield SpanHandle(current)


@overload
def traced(name: Callable[P, R]) -> Callable[P, R]: ...
@overload
def traced(name: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def traced(name: str | Callable[..., Any] | None = None) -> Any:
    """Decorator form of `span()` for functions and coroutines; the span is named after the function by default."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        label = name if isinstance(name, str) else f"{function.__module__}.{function.__qualname__}"
        if inspect.iscoroutinefunction(function):
            coroutine: Callable[P, Awaitable[object]] = function

            @functools.wraps(function)
            async def run_async(*args: P.args, **kwargs: P.kwargs) -> object:
                with span(label):
                    return await coroutine(*args, **kwargs)

            return run_async  # type: ignore[return-value]  # same signature, awaited result

        @functools.wraps(function)
        def run(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(label):
                return function(*args, **kwargs)

        return run

    return decorate(name) if callable(name) else decorate


@contextlib.contextmanager
def trace(
    name: str, *, kind: str = "command", attrs: Mapping[str, object] | None = None, **values: object
) -> Generator[SpanHandle, None, None]:
    """Run a block as its own trace (a script, a consumer, a scheduled job); inside a trace it is a span.

    The trace is analysed and stored when the block ends, like a request.
    """
    if current_trace() is not None:
        with span(name, attrs=attrs, **values) as handle:
            yield handle
        return
    if not _enabled():
        yield SpanHandle(None)
        return
    root_kind = {"task": "task.run", "http": "http", "command": "command"}.get(kind, "function")
    context = start_trace(kind, name, _attrs(attrs, values), root_kind=root_kind)
    started = context.__enter__()
    try:
        yield SpanHandle(started)
    except BaseException as exc:
        from tracepanel.core import errors  # noqa: PLC0415

        # Recorded while the trace is current, then stored like any failed request.
        errors.capture(exc, handled=False)
        context.__exit__(type(exc), exc, exc.__traceback__)
        complete(started)
        raise
    context.__exit__(None, None, None)
    complete(started)


def set_attr(key: str, value: object, *, on_trace: bool = False) -> None:
    """Set an attribute on the current span, or on the whole trace (searchable with the trace) with `on_trace`."""
    target: Trace | Span | None = current_trace() if on_trace else current_span()
    if target is not None:
        target.attrs[_key(key)] = _value(key, value)


def event(name: str, *, attrs: Mapping[str, object] | None = None, **values: object) -> None:
    """Mark a moment on the timeline (an `event` span with no duration), e.g. "cache warmed"."""
    record_span("event", name, start=now_us(), duration=0, attrs=_attrs(attrs, values))


def profile(*, memory: bool = False) -> bool:
    """Profile the rest of the current trace, from here: a flame graph of where its time goes, as the header asks.

    Called in the trace's own code (a view, a task, a command); the tasks it queues and the calls it makes to your
    other services are profiled too. `memory=True` counts its memory as well. The root span's `profile.reason` is
    "code". Whether the trace is profiled now: False outside a trace, or with `profile_enabled = false`.
    """
    return profile_current(MEMORY if memory else TIME)
