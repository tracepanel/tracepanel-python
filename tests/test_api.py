import asyncio

import pytest
from dirty_equals import IsInt, IsPartialDict, IsStr
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

import tracepanel
from tests.agentstore import issues
from tests.profiles import sampled
from tracepanel.core import profile
from tracepanel.core.model import Span, Trace


def shape(trace: Trace) -> list[tuple[str, str, str | None]]:
    """(kind, name, parent name) of every span after the root."""
    by_id = {span.id: span for span in trace.spans}
    return [
        (span.kind, span.name, by_id[span.parent_id].name if span.parent_id in by_id else None)
        for span in trace.spans[1:]
    ]


def named(trace: Trace, name: str) -> Span:
    return next(span for span in trace.spans if span.name == name)


@pytest.mark.django_db
def test_spans_attributes_and_events_inside_a_request(
    client: Client, traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    client.get("/instrumented/")
    (trace,) = traces

    assert [item for item in shape(trace) if item[0] in {"function", "sql", "event"}] == [
        ("function", "pricing.calculate", "tests.project.views.instrumented"),
        ("sql", "SELECT COUNT auth_user", "pricing.calculate"),
        ("event", "cache warmed", "tests.project.views.instrumented"),
    ]
    assert named(trace, "pricing.calculate").attrs == {
        "app.cart_id": 7,
        "shop.region": "eu",
        "app.total": 5,
        "app.items": ["a", "b"],
    }
    assert named(trace, "cache warmed").attrs == {"app.keys": 3}
    assert trace.attrs["app.tenant"] == "acme"
    validator.validate(trace.to_dict())


def test_outside_a_trace_everything_is_a_no_op() -> None:
    with tracepanel.span("nothing", value=1) as span:
        span.set("more", 2)
    tracepanel.set_attr("key", "value")
    tracepanel.event("nobody listens")

    assert span.recording is False


@pytest.mark.django_db
def test_trace_stores_a_trace_of_its_own(traces: list[Trace], users: list[User]) -> None:
    @tracepanel.traced
    def count() -> int:
        return User.objects.count()

    @tracepanel.traced("async.double")
    async def double(value: int) -> int:
        return value * 2

    with tracepanel.trace("sync-prices", source="erp") as root:
        root.set("batch", 3)
        asyncio.run(double(count()))
    (trace,) = traces

    assert (trace.kind, trace.name, trace.status) == ("command", "sync-prices", "ok")
    assert trace.attrs == {
        "app.source": "erp",
        "app.batch": 3,
        "service.name": IsStr,
        "service.role": "command",
        "telemetry.sdk.language": "python",
        "process.runtime.name": "cpython",
        "process.runtime.version": IsStr,
        "tracepanel.framework": IsStr(regex=r"Django \d+\.\d+.*"),
    }
    assert shape(trace) == [
        ("function", "tests.test_api.test_trace_stores_a_trace_of_its_own.<locals>.count", "sync-prices"),
        ("sql", "SELECT COUNT auth_user", "tests.test_api.test_trace_stores_a_trace_of_its_own.<locals>.count"),
        ("function", "async.double", "sync-prices"),
    ]


@pytest.mark.django_db
def test_failing_trace_is_stored_with_its_error(traces: list[Trace]) -> None:
    message = "ERP down"
    with pytest.raises(ConnectionError), tracepanel.trace("sync-prices"):
        raise ConnectionError(message)
    (trace,) = traces

    assert (trace.status, [(error.type, error.handled) for error in trace.errors]) == (
        "error",
        [("builtins.ConnectionError", False)],
    )
    assert [issue.kind for issue in issues(trace)] == ["error"]


@pytest.mark.django_db
def test_trace_inside_a_trace_is_a_span(client: Client, traces: list[Trace]) -> None:
    with tracepanel.trace("outer"), tracepanel.trace("inner"):
        pass
    (trace,) = traces

    assert shape(trace) == [("function", "inner", "outer")]


def test_trace_is_off_outside_debug(traces: list[Trace], settings: Settings) -> None:
    settings.DEBUG = False

    with tracepanel.trace("prod-job") as root:
        root.set("ignored", 1)

    assert (traces, root.recording) == ([], False)


@pytest.mark.django_db
def test_management_command_is_a_trace(traces: list[Trace]) -> None:
    call_command("showmigrations", "auth", verbosity=0)
    (trace,) = traces

    assert (trace.kind, trace.name, trace.spans[0].kind) == ("command", "manage.py showmigrations", "command")
    assert trace.attrs == IsPartialDict({"command.name": "showmigrations"})
    assert len([span for span in trace.spans if span.kind == "sql"]) == IsInt(gt=0)


@pytest.mark.django_db
def test_ignored_commands(traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"ignore_commands": ["show*"]}

    call_command("showmigrations", "auth", verbosity=0)

    assert traces == []


def handed_on() -> dict[str, object]:
    """What a task queued now carries."""
    headers: dict[str, object] = {}
    profile.carry(headers, "tracepanel_profile")
    return headers


def test_profile_profiles_the_rest_of_the_trace_from_code(traces: list[Trace]) -> None:
    with tracepanel.trace("rebuild-index"):
        before = handed_on()
        profiled = tracepanel.profile()
        sampled(0.02)
        after = handed_on()
        again = tracepanel.profile()
    (trace,) = traces

    assert (profiled, again) == (True, True)
    # The tasks it queues and the calls it makes from here ask for a profile too.
    assert (before, after) == ({}, {"tracepanel_profile": "1"})

    assert trace.spans[0].attrs["profile.reason"] == "code"
    assert trace.profile == IsPartialDict({"samples": IsInt(gt=0)})
    assert "memory" not in (trace.profile or {})


def test_profile_with_memory_counts_it_too(traces: list[Trace]) -> None:
    with tracepanel.trace("rebuild-index"):
        tracepanel.profile(memory=True)
        kept = [bytearray(1000) for _ in range(100)]
        sampled(0.02)
        sent = handed_on()
    (trace,) = traces

    assert len(kept) == 100
    assert sent == {"tracepanel_profile": "memory"}

    assert trace.profile == IsPartialDict({"memory": IsPartialDict({"peak": IsInt(ge=0)})})


def test_profile_outside_a_trace_or_with_profiling_off_does_nothing(traces: list[Trace], settings: Settings) -> None:
    assert tracepanel.profile() is False

    settings.TRACEPANEL = {"profile_enabled": False}
    with tracepanel.trace("rebuild-index"):
        profiled = tracepanel.profile()
    (trace,) = traces

    assert profiled is False
    assert (trace.profile, trace.spans[0].attrs) == (None, {})


def test_a_trace_the_setting_profiles_keeps_its_reason(traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"profile": True}

    with tracepanel.trace("rebuild-index"):
        profiled = tracepanel.profile()
    (trace,) = traces

    assert profiled is True
    assert trace.spans[0].attrs["profile.reason"] == "setting"


@pytest.mark.django_db
def test_a_view_profiles_its_request_from_code(client: Client, traces: list[Trace]) -> None:
    client.get("/profiled-by-code/")
    (trace,) = traces

    assert trace.spans[0].attrs["profile.reason"] == "code"
    assert trace.profile == IsPartialDict({"samples": IsInt(gt=0)})
