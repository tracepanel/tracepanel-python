"""tracepanel-agent writes the store the way tracepanel-python does: the same traces, the same rows.

Needs a built agent: $TRACEPANEL_AGENT_BIN, or the debug build of a sibling tracepanel-agent checkout.
"""

import dataclasses
import json
import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Generator
from contextlib import closing
from pathlib import Path

import pytest
from dirty_equals import IsPartialDict, IsPositiveInt
from django.contrib.auth.models import User
from django.test import Client

from tracepanel.core import conf
from tracepanel.core.context import record_span, start_trace
from tracepanel.core.model import Trace
from tracepanel.storage import agent
from tracepanel.storage.store import STORE_VERSION, TABLES, Store

AGENT_BIN = Path(
    os.environ.get(
        "TRACEPANEL_AGENT_BIN",
        Path(__file__).resolve().parents[3] / "tracepanel-agent" / "target" / "debug" / "tracepanel-agent",
    )
)
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not AGENT_BIN.is_file(), reason="no tracepanel-agent build (set TRACEPANEL_AGENT_BIN)"),
]
# Columns holding JSON, compared parsed: Python writes it with spaces, Rust without.
JSON_COLUMNS = {("traces", "doc"), ("logs", "attrs")}


@pytest.fixture
def running_agent(tmp_path: Path) -> Generator[tuple[str, Path, subprocess.Popen[bytes]], None, None]:
    directory = tmp_path / "agent"
    process = subprocess.Popen([AGENT_BIN, "--dir", directory], stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603 (the agent under test)
    addr_file = directory / agent.ADDR_FILE
    deadline = time.monotonic() + 10
    while not addr_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not addr_file.exists():
        process.kill()
        pytest.fail(f"agent did not start: {process.communicate()[1].decode()}")
    yield addr_file.read_text().strip(), directory, process
    if process.poll() is None:
        process.kill()


@pytest.fixture
def requests(client: Client, users: list[User], traces: list[Trace]) -> list[Trace]:
    """Requests of every kind tracepanel stores: N+1 and duplicates, logs, an error, an email, cache, htmx."""
    client.raise_request_exception = False
    for path in ("/n-plus-one/", "/duplicates/", "/logs/", "/error/", "/welcome-email/", "/cache/", "/html/"):
        client.get(path)
    client.get("/htmx/row/", headers={"HX-Request": "true", "HX-Target": "rows"})
    return [*traces, *queued_task()]


def queued_task() -> list[Trace]:
    """A request that queues a task, and the task's run in a worker: linked by task id in the store."""
    with start_trace("http", "POST /orders/") as request:
        record_span(
            "task.enqueue",
            "send_receipt",
            start=request.start,
            duration=50,
            attrs={
                "task.id": "t-1",
                "messaging.system": "redis",
                "server.address": "broker.local",
                "server.port": 6379,
            },
        )
    with start_trace(
        "task",
        "send_receipt",
        {
            "task.id": "t-1",
            "task.state": "SUCCESS",
            "messaging.system": "redis",
            "task.queue_wait": 1200,
        },
    ) as run:
        pass
    return [request, run]


def rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as db:
        tables: dict[str, list[tuple[object, ...]]] = {}
        for table in TABLES:
            if table in {"meta", "logs_fts"}:
                continue
            cursor = db.execute(f"SELECT * FROM {table}")  # noqa: S608 (constant table names)
            columns = [column[0] for column in cursor.description]
            tables[table] = sorted(
                tuple(
                    json.loads(value) if (table, column) in JSON_COLUMNS and value else value
                    for column, value in zip(columns, row, strict=True)
                )
                for row in cursor.fetchall()
            )
        tables["logs_fts"] = sorted(db.execute("SELECT rowid, message, logger FROM logs_fts").fetchall())
    return tables


def test_the_agent_writes_what_python_writes(
    tmp_path: Path, requests: list[Trace], running_agent: tuple[str, Path, subprocess.Popen[bytes]]
) -> None:
    addr, directory, process = running_agent
    python_store = tmp_path / "python" / f"traces.v{STORE_VERSION}.sqlite3"
    Store(python_store).write(requests)

    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=addr))
    for trace in requests:
        writer.submit(trace)
    writer.flush()
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)

    expected = rows(python_store)
    assert {table: len(found) for table, found in expected.items()} == IsPartialDict(
        traces=10,
        issues=IsPositiveInt,
        logs=IsPositiveInt,
        logs_fts=IsPositiveInt,
        sql=IsPositiveInt,
        mails=1,
        peers=IsPositiveInt,
        links=1,
        task_enqueues=1,
        task_runs=1,
    )
    assert writer.sent == len(requests)
    assert rows(directory / f"traces.v{STORE_VERSION}.sqlite3") == expected
    assert not (directory / agent.ADDR_FILE).exists()


def test_without_an_agent_the_process_writes_the_store(tmp_path: Path, requests: list[Trace], store: Store) -> None:
    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=f"unix:{tmp_path}/nobody.sock"))
    for trace in requests:
        writer.submit(trace)
    writer.flush()

    assert (writer.sent, len(store.list_traces(limit=100))) == (0, min(len(requests), store.max_traces))


def test_the_agent_refuses_another_store_version(
    running_agent: tuple[str, Path, subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    addr, _, _ = running_agent

    assert agent.connect(addr, 100) is not None

    # A process of another tracepanel version writes its own store file.
    monkeypatch.setattr(agent, "STORE_VERSION", "0")
    assert agent.connect(addr, 100) is None
