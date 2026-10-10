from collections.abc import Generator

import pytest
from dirty_equals import IsPositiveInt, IsStr

otel_trace = pytest.importorskip("opentelemetry.trace")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402 (optional test group)
from opentelemetry.trace import SpanKind, Status, StatusCode  # noqa: E402

from tests.agentstore import AgentStore  # noqa: E402
from tracepanel.core.context import start_span, start_trace  # noqa: E402
from tracepanel.core.model import Span, Trace  # noqa: E402
from tracepanel.integrations import otel  # noqa: E402

ID = IsStr(regex=r"[0-9A-HJKMNP-TV-Z]{26}")


@pytest.fixture(scope="module")
def tracer() -> Generator[otel_trace.Tracer, None, None]:
    # The provider is global and set once per process: tracepanel joins it when it is set after install().
    otel.install()
    provider = TracerProvider()
    otel_trace.set_tracer_provider(provider)
    yield otel_trace.get_tracer("tests.otel")
    provider.shutdown()


def added(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if "otel.kind" in span.attrs]


def test_spans_join_the_current_trace(tracer: otel_trace.Tracer) -> None:
    with start_trace("http", "GET /orders/") as trace, start_span("view", "orders") as view:
        assert view is not None
        with tracer.start_as_current_span("load orders") as outer:
            outer.set_attribute("db.system", "redis")
            outer.set_attribute("server.address", "cache.local")
            with tracer.start_as_current_span("GET", kind=SpanKind.CLIENT) as call:
                call.set_attributes({"http.request.method": "GET", "server.address": "api.example.com"})
                call.set_status(Status(StatusCode.ERROR))

    call_span, outer_span = added(trace)
    assert outer_span.to_dict() == {
        "id": ID,
        "parent_id": view.id,
        "kind": "function",
        "name": "load orders",
        "start": IsPositiveInt,
        "duration": IsPositiveInt | 0,
        "status": "ok",
        "attrs": {
            "db.system": "redis",
            "server.address": "cache.local",
            "otel.kind": "internal",
            "otel.scope": "tests.otel",
        },
    }

    assert call_span.to_dict() == {
        "id": ID,
        "parent_id": outer_span.id,
        "kind": "http.client",
        "name": "GET",
        "start": IsPositiveInt,
        "duration": IsPositiveInt | 0,
        "status": "error",
        "attrs": {
            "http.request.method": "GET",
            "server.address": "api.example.com",
            "otel.kind": "client",
            "otel.scope": "tests.otel",
        },
    }


def test_what_tracepanel_records_itself_is_left_out(tracer: otel_trace.Tracer) -> None:
    django = otel_trace.get_tracer("opentelemetry.instrumentation.django")
    with start_trace("http", "GET /") as trace:
        with django.start_as_current_span("GET /"):
            pass
        with tracer.start_as_current_span("request", kind=SpanKind.SERVER):
            pass

    assert added(trace) == []


def test_outside_a_trace_nothing_is_kept(tracer: otel_trace.Tracer) -> None:
    with tracer.start_as_current_span("background job"):
        pass

    assert otel.PROCESSOR._open == {}  # noqa: SLF001


def test_a_provider_set_before_install_is_joined_too() -> None:
    provider = TracerProvider()
    otel._join(provider)  # noqa: SLF001
    otel._join(provider)  # noqa: SLF001

    assert provider._active_span_processor._span_processors == (otel.PROCESSOR,)  # noqa: SLF001


def test_services_they_call_are_on_the_map(tracer: otel_trace.Tracer, store: AgentStore) -> None:
    with start_trace("http", "GET /orders/") as trace:
        with tracer.start_as_current_span("GET", kind=SpanKind.CLIENT) as call:
            call.set_attributes(
                {"http.request.method": "GET", "url.scheme": "https", "server.address": "api.example.com"}
            )
        with tracer.start_as_current_span("HGET", kind=SpanKind.CLIENT) as command:
            command.set_attributes({"db.system": "redis", "server.address": "cache.local", "server.port": 6379})

    store.write([trace])

    assert [
        (node["kind"], node["system"], node["address"], node["port"]) for node in store.list_services()["services"]
    ] == [
        ("database", "redis", "cache.local", 6379),
        ("http", "https", "api.example.com", None),
    ]
