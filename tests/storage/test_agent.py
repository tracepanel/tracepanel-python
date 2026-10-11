"""Sending traces to tracepanel-agent: over a unix socket or TCP, and what happens without one.

The agent under test is the one uv built from ../tracepanel (or $TRACEPANEL_AGENT_BIN).
"""

import dataclasses
import signal
import subprocess
import time
from pathlib import Path

import pytest
from dirty_equals import IsStr
from django.contrib.auth.models import User
from django.test import Client

from tests.agentstore import AgentStore
from tracepanel.console import proxy
from tracepanel.core import conf
from tracepanel.core.context import record_span, start_trace
from tracepanel.core.model import Trace
from tracepanel.storage import agent
from tracepanel.storage.transport import set_writer

pytestmark = pytest.mark.django_db


def requests(count: int) -> list[Trace]:
    made = []
    for index in range(count):
        with start_trace("http", f"GET /{index}/") as trace:
            record_span("sql", "SELECT t", start=trace.start, duration=10, attrs={"db.fingerprint": "SELECT 1"})
        made.append(trace)
    return made


@pytest.mark.parametrize("listen", ["unix", "tcp:127.0.0.1:0"])
def test_the_sdk_sends_its_traces_to_the_agent(tmp_path: Path, listen: str) -> None:
    # TCP: Windows has no unix sockets; an app in a container reaches the agent on its host.
    store = AgentStore.start(tmp_path / ".tracepanel", "--listen", listen)
    sent = requests(3)
    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=store.address))
    for trace in sent:
        writer.submit(trace)
    writer.flush()
    store.wait_for(sent[-1].id)

    assert store.address.startswith("unix:" if listen == "unix" else "tcp:127.0.0.1:")

    assert (writer.sent, writer.dropped) == (3, 0)
    assert sorted(str(row["name"]) for row in store.list_traces()) == ["GET /0/", "GET /1/", "GET /2/"]

    # Stopped, it leaves no address behind: SDKs don't send to a socket nobody listens on.
    store.process.send_signal(signal.SIGTERM)
    store.process.wait(timeout=10)
    assert not (store.directory / agent.ADDR_FILE).exists()


def test_without_an_agent_traces_are_dropped_and_the_terminal_says_why_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=f"unix:{tmp_path}/nobody.sock"))
    for trace in requests(2):
        writer.submit(trace)
        writer.flush()

    assert (writer.sent, writer.dropped) == (0, 2)

    assert capsys.readouterr().err == IsStr(regex=r"tracepanel ▸ the agent isn't running \(see .*agent\.log\).*\n")


def test_the_agent_refuses_another_protocol(store: AgentStore, monkeypatch: pytest.MonkeyPatch) -> None:
    assert agent.connect(store.address, 100) is not None

    monkeypatch.setattr(agent, "PROTOCOL", 0)
    assert agent.connect(store.address, 100) is None


def test_a_second_agent_on_a_store_is_refused(store: AgentStore) -> None:
    with pytest.raises(RuntimeError, match="an agent already runs for"):
        AgentStore.start(store.directory)


def test_the_apps_console_is_the_agents(store: AgentStore, client: Client, users: list[User]) -> None:
    (trace,) = requests(1)
    store.write([trace])

    page = client.get("/__tracepanel__/")
    widget = client.get("/__tracepanel__/widget.min.js")
    api = client.get(f"/__tracepanel__/api/traces/{trace.id}")

    assert (page.status_code, b"<html" in page.content.lower()) == (200, True)
    assert (widget.status_code, widget["Cache-Control"]) == (200, "no-cache")
    assert (api.status_code, api.json()["name"]) == (200, "GET /0/")

    # What only the app can do stays in the app.
    assert client.get("/__tracepanel__/api/users/me").status_code == 200

    # The agent gone: the console says why instead of an empty page.
    store.stop()
    assert client.get(f"/__tracepanel__/api/traces/{trace.id}").status_code == 503


def test_a_trace_still_on_its_way_is_waited_for(store: AgentStore, client: Client) -> None:
    # The widget asks for its page's issues as soon as the page loads: the trace may still be in this process's queue.
    writer = agent.AgentWriter(store.config())
    set_writer(writer)
    try:
        first, second = requests(2)
        writer.submit(first)
        writer.submit(second)
        rows = client.get("/__tracepanel__/api/traces", {"ids": f"{first.id},{second.id}"}).json()["traces"]
        single = client.get(f"/__tracepanel__/api/traces/{second.id}")
    finally:
        set_writer(None)

    assert sorted(row["id"] for row in rows) == sorted([first.id, second.id])
    assert (single.status_code, single.json()["id"]) == (200, second.id)

    # Not a trace on its way: an empty group's summary answers at once.
    started = time.monotonic()
    assert client.get("/__tracepanel__/api/traces/summary", {"q": "route:/nowhere/"}).status_code == 404
    assert time.monotonic() - started < proxy.TRACE_WAIT_S


def test_an_agent_that_died_is_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Killed, it left its addresses behind: nobody answers there, so the app starts a new agent.
    directory = tmp_path / ".tracepanel"
    directory.mkdir()
    (directory / agent.ADDR_FILE).write_text(f"unix:{tmp_path}/gone.sock\n")
    (directory / agent.CONSOLE_FILE).write_text("http://127.0.0.1:9/\n")
    config = dataclasses.replace(conf.get_config(), agent="auto", dir=str(directory))
    started: list[subprocess.Popen[bytes]] = []
    start = agent.start

    def recorded(config: conf.Config) -> subprocess.Popen[bytes] | None:
        if (process := start(config)) is not None:
            started.append(process)
        return process

    monkeypatch.setattr(agent, "can_start", lambda _config: True)  # pyrefly: ignore[implicit-any-lambda]
    monkeypatch.setattr(agent, "_start_after", 0.0)
    monkeypatch.setattr(agent, "start", recorded)

    assert agent.ensure_started(config) is True

    try:
        assert (agent.answers(agent.address(config)), agent.console_url(config)) == (
            True,
            IsStr(regex=r"http://127\.0\.0\.1:\d+/"),
        )
        assert agent.console_url(config) != "http://127.0.0.1:9/"

        assert capsys.readouterr().err == f"tracepanel ▸ agent started for {directory}\n"
    finally:
        for process in started:
            process.terminate()
            process.wait(timeout=10)
