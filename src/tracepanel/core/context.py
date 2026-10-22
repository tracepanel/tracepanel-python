"""Current trace and span via contextvars (works for async too)."""

import asyncio
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from tracepanel.core import profile as profiling
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Span, Trace

MAX_SPANS = 2000
# A call made synchronously on a running event loop's thread: every other task on the loop waited for it.
BLOCKS_EVENT_LOOP = "code.blocks_event_loop"

_trace: ContextVar[Trace | None] = ContextVar("tracepanel_trace", default=None)
_span: ContextVar[Span | None] = ContextVar("tracepanel_span", default=None)


def now_us() -> int:
    return time.time_ns() // 1000


def on_event_loop() -> bool:
    """Whether a synchronous call made now blocks an event loop: its thread runs one (an async view, an endpoint).

    A thread a sync view or sync_to_async runs in has no loop running: its calls block nothing else.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def blocking_attrs() -> Attrs:
    """`code.blocks_event_loop` for a synchronous call made on an event loop's thread; nothing otherwise."""
    return {BLOCKS_EVENT_LOOP: True} if on_event_loop() else {}


def current_trace() -> Trace | None:
    return _trace.get()


def current_span() -> Span | None:
    return _span.get()


@contextmanager
def start_trace(
    kind: str, name: str, attrs: Attrs | None = None, *, root_kind: str | None = None, profile: bool = False
) -> Generator[Trace, None, None]:
    """Run the block as a new trace whose root span has the same name (and kind, unless `root_kind` says).

    `profile`: the request asked to be profiled (the setting profiles every trace anyway).
    """
    start = now_us()
    started = time.perf_counter_ns()
    trace = Trace(id=new_id(), kind=kind, name=name, start=start, attrs=dict(attrs or {}))
    root = Span(id=new_id(), parent_id=None, kind=root_kind or kind, name=name, start=start)
    trace.spans.append(root)
    trace_token, span_token = _trace.set(trace), _span.set(root)
    recording = profiling.SAMPLER.start() if profiling.wanted(asked=profile) else None
    recording_token = profiling.current.set(recording)
    try:
        yield trace
    except BaseException:
        trace.status = root.status = "error"
        raise
    finally:
        trace.duration = root.duration = (time.perf_counter_ns() - started) // 1000
        profiling.current.reset(recording_token)
        if recording is not None:
            trace.profile = profiling.SAMPLER.stop(recording)
        root.name = trace.name
        _span.reset(span_token)
        _trace.reset(trace_token)


@contextmanager
def holding(trace: Trace) -> Generator[None, None, None]:
    """Record into `trace`, outside any other: work done before the trace it belongs to begins (request_started).

    Its spans have no parent: whoever adopts them later puts them under its root.
    """
    trace_token, span_token = _trace.set(trace), _span.set(None)
    try:
        yield
    finally:
        _span.reset(span_token)
        _trace.reset(trace_token)


@contextmanager
def start_span(kind: str, name: str, attrs: Attrs | None = None) -> Generator[Span | None, None, None]:
    """Run the block as a child span of the current one; a no-op outside a trace."""
    trace = _trace.get()
    if trace is None or len(trace.spans) >= MAX_SPANS:
        yield None
        return
    parent = _span.get()
    span = Span(
        id=new_id(),
        parent_id=parent.id if parent else None,
        kind=kind,
        name=name,
        start=now_us(),
        attrs=dict(attrs or {}),
    )
    trace.spans.append(span)
    started = time.perf_counter_ns()
    token = _span.set(span)
    try:
        yield span
    except BaseException:
        span.status = "error"
        raise
    finally:
        span.duration = (time.perf_counter_ns() - started) // 1000
        _span.reset(token)


def set_current_span(span: Span | None) -> None:
    """Make the span current in this context: one a worker thread left current, as a call made here would have."""
    _span.set(span)


@dataclass(slots=True, eq=False)
class OpenSpan:
    """A span one event begins and another ends (SQLAlchemy's BEGIN and COMMIT), perhaps in another context.

    It is current from its start, as a `with start_span()` block's span is. Ending it makes the span it began under
    current again, in whatever context it ends: a worker thread's copy of the request's context has no token to reset.
    """

    span: Span
    parent: Span | None
    started: int

    def end(self) -> None:
        self.span.duration = (time.perf_counter_ns() - self.started) // 1000
        if _span.get() is self.span:
            _span.set(self.parent)


def open_span(kind: str, name: str, attrs: Attrs | None = None) -> OpenSpan | None:
    """Begin a span, current until its end(): a child of the current one; None outside a trace."""
    trace = _trace.get()
    if trace is None or len(trace.spans) >= MAX_SPANS:
        return None
    parent = _span.get()
    span = Span(
        id=new_id(),
        parent_id=parent.id if parent else None,
        kind=kind,
        name=name,
        start=now_us(),
        attrs=dict(attrs or {}),
    )
    trace.spans.append(span)
    _span.set(span)
    return OpenSpan(span, parent, time.perf_counter_ns())


def record_span(  # noqa: PLR0913
    kind: str,
    name: str,
    *,
    start: int,
    duration: int,
    attrs: Attrs,
    status: str = "ok",
    span_id: str | None = None,
    parent: Span | None = None,
) -> Span | None:
    """Append a finished leaf span to the current trace; cheaper than start_span for hot paths like SQL.

    Its parent is the current span, unless `parent` says (a statement's transaction, open on its connection).
    """
    trace = _trace.get()
    if trace is None:
        return None
    if len(trace.spans) >= MAX_SPANS:
        dropped = trace.attrs.get("tracepanel.dropped_spans")
        trace.attrs["tracepanel.dropped_spans"] = (dropped if isinstance(dropped, int) else 0) + 1
        return None
    parent = parent or _span.get()
    span = Span(
        id=span_id or new_id(),
        parent_id=parent.id if parent else None,
        kind=kind,
        name=name,
        start=start,
        duration=duration,
        status=status,
        attrs=attrs,
    )
    trace.spans.append(span)
    return span


def recorded() -> int:
    """How many spans the current trace has: a mark for adopt()."""
    trace = _trace.get()
    return len(trace.spans) if trace is not None else 0


def adopt(span_id: str, since: int) -> None:
    """Move the spans recorded under the current span since `since` (recorded()) under `span_id`.

    For a span recorded once its work is over (a task enqueue, from the queue's signal after publishing): what the
    work did meanwhile (the broker's Redis commands) becomes its children, as if it had been open all along.
    """
    trace = _trace.get()
    if trace is None:
        return
    parent = _span.get()
    current = parent.id if parent else None
    for span in trace.spans[since:]:
        if span.parent_id == current and span.id != span_id:
            span.parent_id = span_id
