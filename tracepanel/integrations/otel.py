"""OpenTelemetry: spans from OTel instrumentations (httpx, requests, redis, boto3, grpc…) in tracepanel's traces.

A span processor added to the application's TracerProvider: a span that starts inside a request, task or job
tracepanel traces becomes a span of that trace, under the span that was current when it started (or under its OTel
parent). The app configures OpenTelemetry as usual (a TracerProvider, `opentelemetry-instrument`); tracepanel joins
the provider that is set now or later, and never sets one itself, so the app's exporters keep working.

What tracepanel records itself is left out (the frameworks, SQL, Celery, logging): those spans would show twice. The
mapping (`from_otel`) knows only OpenTelemetry's span model, not this SDK: it is the one any OTLP source would use.
"""

from collections.abc import Mapping, Sequence

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.trace import SpanKind, StatusCode

from tracepanel._compat import override
from tracepanel.core.context import MAX_SPANS, current_span, current_trace
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, AttrValue, Span, Trace
from tracepanel.core.patching import patch, patched

MARKER = "_tracepanel_processor"
# Instrumentations of what tracepanel records itself: their spans would duplicate tracepanel's own.
COVERED = frozenset(
    f"opentelemetry.instrumentation.{name}"
    for name in (
        "django",
        "wsgi",
        "asgi",
        "flask",
        "fastapi",
        "starlette",
        "falcon",
        "dbapi",
        "sqlite3",
        "psycopg",
        "psycopg2",
        "sqlalchemy",
        "celery",
        "logging",
    )
)
# OTel spans started but not ended yet; bounded in case some never end.
OPEN_MAX = 10_000


def _value(value: object) -> AttrValue:
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, Sequence):
        return [item if isinstance(item, (str, bool, int, float)) else str(item) for item in value]
    return str(value)


def kind_of(otel_kind: SpanKind, attrs: Mapping[str, object]) -> str:
    """The span kind in tracepanel for an OTel span: an outgoing HTTP call is `http.client`, the rest a `function`."""
    http = "http.request.method" in attrs or "http.method" in attrs
    return "http.client" if otel_kind is SpanKind.CLIENT and http else "function"


def from_otel(  # noqa: PLR0913 (an OTel span's fields)
    *,
    span_id: str,
    parent_id: str | None,
    name: str,
    otel_kind: SpanKind,
    start_ns: int,
    end_ns: int,
    error: bool,
    attributes: Mapping[str, object],
    scope: str | None,
) -> Span:
    """An OTel span as a tracepanel span: its attributes as they are (both follow the semantic conventions)."""
    attrs: Attrs = {key: _value(value) for key, value in attributes.items()}
    attrs["otel.kind"] = otel_kind.name.lower()
    if scope:
        attrs["otel.scope"] = scope
    return Span(
        id=span_id,
        parent_id=parent_id,
        kind=kind_of(otel_kind, attrs),
        name=name,
        start=start_ns // 1000,
        duration=max(end_ns - start_ns, 0) // 1000,
        status="error" if error else "ok",
        attrs=attrs,
    )


def _scope(span: ReadableSpan) -> str | None:
    scope = span.instrumentation_scope
    return scope.name if scope is not None else None


class TracepanelProcessor(SpanProcessor):
    """Copies OTel spans into the tracepanel trace they started in."""

    def __init__(self) -> None:
        # OTel span id -> (the trace, the tracepanel id given to the span, its tracepanel parent).
        self._open: dict[int, tuple[Trace, str, str | None]] = {}

    @override
    def on_start(self, span: SdkSpan, parent_context: object = None) -> None:  # noqa: ARG002 (SpanProcessor API)
        trace = current_trace()
        if trace is None or span.kind is SpanKind.SERVER or _scope(span) in COVERED:
            # A server span is a request: the trace itself, which tracepanel opened.
            return
        parent = span.parent
        mapped = self._open.get(parent.span_id) if parent is not None else None
        if mapped is not None and mapped[0] is trace:
            parent_id: str | None = mapped[1]
        else:
            current = current_span()
            parent_id = current.id if current is not None else None
        if len(self._open) >= OPEN_MAX:
            self._open.clear()
        self._open[span.get_span_context().span_id] = (trace, new_id(), parent_id)

    @override
    def on_end(self, span: ReadableSpan) -> None:
        context = span.context
        entry = self._open.pop(context.span_id, None) if context is not None else None
        if entry is None:
            return
        trace, span_id, parent_id = entry
        # Ended after its trace was sent (work the request left running): too late to add.
        if current_trace() is not trace or len(trace.spans) >= MAX_SPANS:
            return
        trace.spans.append(
            from_otel(
                span_id=span_id,
                parent_id=parent_id,
                name=span.name,
                otel_kind=span.kind,
                start_ns=span.start_time or 0,
                end_ns=span.end_time or 0,
                error=span.status.status_code is StatusCode.ERROR,
                attributes=span.attributes or {},
                scope=_scope(span),
            )
        )

    @override
    def shutdown(self) -> None:
        self._open.clear()

    @override
    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002 (SpanProcessor API)
        return True


PROCESSOR = TracepanelProcessor()


def _join(provider: object) -> None:
    if isinstance(provider, TracerProvider) and not getattr(provider, MARKER, False):
        provider.add_span_processor(PROCESSOR)
        setattr(provider, MARKER, True)


def _set_tracer_provider(provider: otel_trace.TracerProvider) -> None:
    _original_set(provider)
    _join(otel_trace.get_tracer_provider())


_original_set = otel_trace.set_tracer_provider


def install() -> None:
    """Join the TracerProvider the app set, and the one it sets later."""
    _join(otel_trace.get_tracer_provider())
    if not patched(otel_trace, "set_tracer_provider"):
        patch(otel_trace, "set_tracer_provider", _set_tracer_provider)
