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


@dataclass(slots=True, eq=False)
class _Outer:
    """What a trace's end makes current again: the span current where it began, and the outer trace's own _Outer.

    A trace can begin or end inside a span of another trace's: a task run, begun in one Django Tasks signal's
    receiver and ended in another's, each send a `signal` span. The span that was current where the trace began can
    end before the trace does: its parent is current again once the trace ends (`_over`).
    """

    span: Span | None
    previous: "_Outer | None"


_outer: ContextVar[_Outer | None] = ContextVar("tracepanel_outer", default=None)


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


class TraceBlock:
    """A block run as a new trace (start_trace): current from __enter__ until __exit__.

    An exception raised from outside can stop the code anywhere: a signal handler's (a time limit's) or another
    thread's. Entering is all or nothing: stopped halfway, it puts the context back as it was. Exiting puts back what
    entering found, so it can be done again when an exit was itself cut short; exiting a block that never entered, or
    one exited already, does nothing.
    """

    def __init__(self, kind: str, name: str, attrs: Attrs | None, *, root_kind: str | None, profile: bool) -> None:
        self.kind, self.name, self.attrs, self.root_kind, self.profile = kind, name, attrs, root_kind, profile
        self.trace: Trace | None = None
        self._started = 0
        self._recording: profiling.Recording | None = None
        self._saved: _Saved | None = None

    def __enter__(self) -> Trace:
        start = now_us()
        self._started = time.perf_counter_ns()
        trace = Trace(id=new_id(), kind=self.kind, name=self.name, start=start, attrs=dict(self.attrs or {}))
        root = Span(id=new_id(), parent_id=None, kind=self.root_kind or self.kind, name=self.name, start=start)
        trace.spans.append(root)
        saved = _Saved(_trace.get(), _Outer(_span.get(), _outer.get()), profiling.current.get())
        try:
            _trace.set(trace)
            _outer.set(saved.outer)
            _span.set(root)
            self._recording = profiling.SAMPLER.start() if profiling.wanted(asked=self.profile) else None
            profiling.current.set(self._recording)
        except BaseException:
            saved.put_back()
            if self._recording is not None:
                profiling.SAMPLER.stop(self._recording)
            raise
        self.trace, self._saved = trace, saved
        return trace

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        saved, trace = self._saved, self.trace
        if saved is None or trace is None:
            return
        root = trace.spans[0]
        if exc_type is not None:
            trace.status = root.status = "error"
        trace.duration = root.duration = (time.perf_counter_ns() - self._started) // 1000
        recording, self._recording = self._recording, None
        if recording is not None:
            trace.profile = profiling.SAMPLER.stop(recording)
        root.name = trace.name
        saved.put_back()
        self._saved = None


@dataclass(slots=True, eq=False)
class _Saved:
    """What was current before a trace began: its end, or a beginning cut short, makes it current again."""

    trace: Trace | None
    outer: _Outer
    recording: profiling.Recording | None

    def put_back(self) -> None:
        _trace.set(self.trace)
        _outer.set(self.outer.previous)
        # The span current where the trace began, or its parent when it ended meanwhile (_over).
        _span.set(self.outer.span)
        profiling.current.set(self.recording)


def start_trace(
    kind: str, name: str, attrs: Attrs | None = None, *, root_kind: str | None = None, profile: bool = False
) -> TraceBlock:
    """Run the block as a new trace whose root span has the same name (and kind, unless `root_kind` says).

    `profile`: the request asked to be profiled (the setting profiles every trace anyway).
    """
    return TraceBlock(kind, name, attrs, root_kind=root_kind, profile=profile)


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
        if _trace.get() is trace:
            _span.reset(token)
        else:
            # A trace began or ended inside the block: the current span is that trace's, or its outer's, not ours.
            _over(span, parent)


def _over(span: Span, parent: Span | None) -> None:
    """`span` ended while a trace begun inside it is still running: that trace's end makes `parent` current."""
    outer = _outer.get()
    while outer is not None:
        if outer.span is span:
            outer.span = parent
        outer = outer.previous


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
        else:
            _over(self.span, self.parent)


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
