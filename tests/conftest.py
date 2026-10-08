import json
from collections.abc import Generator
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from jsonschema import Draft202012Validator

from tracepanel.core import conf
from tracepanel.core.model import Trace
from tracepanel.storage import store as store_module
from tracepanel.storage import transport
from tracepanel.storage.store import Store

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "trace.schema.json"


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
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    store = Store(tmp_path / "traces.sqlite3", max_traces=5)
    monkeypatch.setattr(store_module, "_store", store)
    return store


@pytest.fixture
def users(db: None) -> list[User]:
    group = Group.objects.create(name="staff")
    created = [User.objects.create_user(f"user{i}", password="x") for i in range(5)]
    for user in created:
        user.groups.add(group)
    return created


@pytest.fixture(autouse=True)
def fresh_config() -> Generator[None, None, None]:
    """Each test reads its own configuration (tracepanel caches it; variables set by a test must not leak)."""
    yield
    conf.reset()
