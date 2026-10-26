import asyncio

import pytest
from dirty_equals import IsInt, IsPartialDict, IsStr
from django.test import AsyncClient, Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tests.profiles import share
from tracepanel.core import conf
from tracepanel.core.model import Trace
from tracepanel.storage import agent

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


def test_a_sync_view_under_an_asgi_server_is_profiled_in_the_thread_that_runs_it(traces: list[Trace]) -> None:
    # As uvicorn serves it: the event loop in this thread, the view in sync_to_async's worker thread.
    response = asyncio.run(AsyncClient().get("/busy/", headers={"X-Tracepanel-Profile": "1"}))
    (trace,) = traces

    assert response.status_code == 200
    assert {"busy", "spin"} <= in_app(trace)
    # The loop's thread, waiting in its selector meanwhile, is not what the profile shows.
    assert share(trace, "spin") > 0.3


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


def test_a_request_asking_for_memory_has_it(client: Client, traces: list[Trace]) -> None:
    client.get("/busy/", headers={"X-Tracepanel-Profile": "memory"})
    client.cookies["tracepanel_profile"] = "memory"
    client.get("/busy/")
    client.cookies["tracepanel_profile"] = "1"
    client.get("/busy/")
    by_header, by_cookie, time_only = traces

    assert by_header.profile is not None
    assert by_header.profile["memory"] == IsPartialDict({"peak": IsInt(ge=0), "sites_at": IsStr()})
    assert by_cookie.profile is not None
    assert "memory" in by_cookie.profile

    assert time_only.profile is not None
    assert "memory" not in time_only.profile
    assert "gc" in time_only.profile


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


def test_the_hello_offers_profiling_while_it_is_on(settings: Settings) -> None:
    offered = agent.capabilities(conf.get_config())
    settings.TRACEPANEL = {"profile_enabled": False}

    assert {"profile", "profile_memory"} <= set(offered)
    assert not {"profile", "profile_memory"} & set(agent.capabilities(conf.get_config()))


def test_the_hello_says_when_the_settings_profile_every_request(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing sent beside the traces (Celery's task events, when it is installed).
    monkeypatch.setattr(agent, "SENDS", set())
    default = agent.capabilities(conf.get_config())
    settings.TRACEPANEL = {"profile": True}
    every = agent.capabilities(conf.get_config())
    settings.TRACEPANEL = {"profile_memory": True}
    memory = agent.capabilities(conf.get_config())
    settings.TRACEPANEL = {"profile": True, "profile_memory": True, "profile_enabled": False}
    off = agent.capabilities(conf.get_config())

    assert default == ["profile", "profile_memory", "request"]
    assert every == ["profile", "profile_all", "profile_memory", "request"]
    assert memory == ["profile", "profile_memory", "profile_memory_all", "request"]
    assert off == ["request"]
