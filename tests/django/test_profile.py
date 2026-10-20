import pytest
from dirty_equals import IsInt
from django.test import Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tracepanel.core.model import Trace

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def often(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sample every millisecond: the view runs for 150 ms.
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")


def in_app(trace: Trace) -> set[str]:
    """The project's functions the trace's profile names."""
    assert trace.profile is not None
    frames = trace.profile["frames"]
    assert isinstance(frames, list)
    return {str(frame["function"]) for frame in frames if isinstance(frame, dict) and frame["in_app"]}


def test_a_request_asking_by_header_is_profiled(
    client: Client, traces: list[Trace], validator: Draft202012Validator
) -> None:
    response = client.get("/busy/", headers={"X-Tracepanel-Profile": "1"})
    (trace,) = traces

    assert response.status_code == 200
    assert trace.profile is not None
    assert trace.profile["interval_us"] == 1000
    assert trace.profile["samples"] == IsInt(gt=0)
    assert {"busy", "spin"} <= in_app(trace)
    validator.validate(trace.to_dict())
    assert "Set-Cookie" not in response.headers


def test_the_panels_cookie_profiles_each_request_while_it_is_set(client: Client, traces: list[Trace]) -> None:
    client.cookies["tracepanel_profile"] = "1"

    response = client.get("/busy/")
    client.get("/busy/")
    del client.cookies["tracepanel_profile"]
    client.get("/busy/")
    first, second, after = traces

    assert "spin" in in_app(first)
    assert "spin" in in_app(second)
    assert "tracepanel_profile" not in response.cookies

    assert after.profile is None


def test_a_request_is_not_profiled_unless_asked(client: Client, traces: list[Trace]) -> None:
    client.get("/busy/")
    (trace,) = traces

    assert trace.profile is None
    assert "profile" not in trace.to_dict()


def test_the_setting_profiles_every_request(client: Client, traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"profile": True}

    client.get("/busy/")
    (trace,) = traces

    assert "spin" in in_app(trace)


def test_with_profiling_off_nothing_is_profiled(client: Client, traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"profile_enabled": False}

    client.get("/busy/", headers={"X-Tracepanel-Profile": "1"})
    (trace,) = traces

    assert trace.profile is None


def test_the_console_offers_profiling_while_it_is_on(client: Client, settings: Settings) -> None:
    offered = client.get("/__tracepanel__/api/capabilities").json()
    settings.TRACEPANEL = {"profile_enabled": False}
    turned_off = client.get("/__tracepanel__/api/capabilities").json()

    assert offered == {"explain": True, "users": True, "profile": True, "request": True}
    assert turned_off == {"explain": True, "users": True, "profile": False, "request": True}
