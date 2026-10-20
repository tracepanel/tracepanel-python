import json
from collections.abc import Generator
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from jsonschema import Draft202012Validator

from tests.agentstore import AGENT_SCHEMAS, AgentStore
from tracepanel.core import conf
from tracepanel.core.model import Trace
from tracepanel.storage import agent, transport

# What an SDK sends the agent: the agent's schema/trace.schema.json.
SCHEMA = AGENT_SCHEMAS / "trace.schema.json"


class Collector:
    def __init__(self) -> None:
        self.traces: list[Trace] = []

    def submit(self, trace: Trace) -> None:
        self.traces.append(trace)

    def flush(self, timeout: float = 5.0) -> None:
        pass


@pytest.fixture
def traces() -> Generator[list[Trace], None, None]:
    collector = Collector()
    previous = transport._writer  # noqa: SLF001
    transport.set_writer(collector)
    yield collector.traces
    transport.set_writer(previous)


@pytest.fixture(scope="session")
def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA.read_text()))


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[AgentStore, None, None]:
    """The project's store: an agent of its own, which the app's console (/__tracepanel__/) forwards to."""
    directory = tmp_path / ".tracepanel"
    running = AgentStore.start(directory)
    monkeypatch.setenv("TRACEPANEL_DIR", str(directory))
    monkeypatch.setenv("TRACEPANEL_AGENT", "auto")
    conf.reset()
    yield running
    running.stop()


@pytest.fixture
def app_console(store: AgentStore, monkeypatch: pytest.MonkeyPatch) -> AgentStore:
    """The store's agent, and this app connected to it: what the app's console offers comes from its hello."""
    if not store.offers_at_app_consoles():
        pytest.skip("the released agent offers nothing an app answers at its console: a newer one does")
    monkeypatch.setattr(transport, "_writer", agent.AgentWriter(store.config()))
    return store


@pytest.fixture
def users(db: None) -> list[User]:
    group = Group.objects.create(name="staff")
    created = [User.objects.create_user(f"user{i}", password="x") for i in range(5)]
    for user in created:
        user.groups.add(group)
    return created


@pytest.fixture(autouse=True)
def fresh_config(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Each test reads its own configuration (tracepanel caches it; variables set by a test must not leak).

    No agent: one running for this checkout would get the tests' traces and answer their console calls.
    """
    monkeypatch.setenv("TRACEPANEL_AGENT", "off")
    conf.reset()
    yield
    conf.reset()
