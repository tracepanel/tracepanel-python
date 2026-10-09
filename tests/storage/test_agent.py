"""Sending traces to tracepanel-agent: over a unix socket or TCP, and what happens without one.

The agent under test is the one uv built from ../tracepanel-agent (or $TRACEPANEL_AGENT_BIN).
"""

import dataclasses
import signal
from pathlib import Path

import pytest
from dirty_equals import IsStr
from django.contrib.auth.models import User
from django.test import Client

from tests.agentstore import AgentStore
from tracepanel.core import conf
from tracepanel.core.context import record_span, start_trace
from tracepanel.core.model import Trace
from tracepanel.storage import agent

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
    widget = client.get("/__tracepanel__/widget.js")
    api = client.get(f"/__tracepanel__/api/traces/{trace.id}")

    assert (page.status_code, b"<html" in page.content.lower()) == (200, True)
    assert (widget.status_code, widget["Cache-Control"]) == (200, "no-cache")
    assert (api.status_code, api.json()["name"]) == (200, "GET /0/")

    # What only the app can do stays in the app.
    assert client.get("/__tracepanel__/api/users/me").status_code == 200

    # The agent gone: the console says why instead of an empty page.
    store.stop()
    assert client.get(f"/__tracepanel__/api/traces/{trace.id}").status_code == 503
