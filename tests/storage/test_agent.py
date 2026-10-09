"""tracepanel-agent writes the store the way tracepanel-python does: the same traces, the same rows.

Needs a built agent: $TRACEPANEL_AGENT_BIN, or the debug build of a sibling tracepanel-agent checkout.
"""

import dataclasses
import importlib.util
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import pytest
from dirty_equals import IsPartialDict, IsPositiveInt
from django.contrib.auth.models import User
from django.test import Client

from tracepanel.core import conf
from tracepanel.core.analysis import analyze
from tracepanel.core.context import record_span, start_span, start_trace
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
    # Skipped without a build, unless a build was named (CI): then a missing one fails the tests.
    pytest.mark.skipif(
        not AGENT_BIN.is_file() and "TRACEPANEL_AGENT_BIN" not in os.environ,
        reason="no tracepanel-agent build (set TRACEPANEL_AGENT_BIN)",
    ),
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


@pytest.fixture
def with_issues(client: Client, requests: list[Trace], traces: list[Trace]) -> list[Trace]:
    """Every kind of issue, as tracepanel-python finds them.

    The requests' own, and a slow query and an email in a long transaction (a test's own transaction makes a view's
    a savepoint, which is no issue).
    """
    client.get("/same-group/")
    client.get("/htmx/login-needed/", headers={"HX-Request": "true"}, follow=True)
    with start_trace("http", "POST /reports/") as slow:
        with start_span("transaction", "transaction default", {"db.alias": "default"}) as outer:
            assert outer is not None
            record_span(
                "sql",
                "SELECT reports",
                start=slow.start,
                duration=250_500,
                attrs={
                    "db.fingerprint": "SELECT * FROM reports",
                    "db.params_hash": "a",
                    "db.alias": "default",
                    "code.origin": "app/views.py:9 in report",
                },
            )
            record_span(
                "mail",
                "Report ready",
                start=slow.start,
                duration=900,
                attrs={"mail.subject": "Report ready", "code.origin": "app/views.py:12 in report"},
            )
        outer.duration = 700_000
    slow.issues = analyze(slow)
    return [*requests, *[t for t in traces if t not in requests], slow]


def test_the_agent_finds_the_issues_python_finds(
    tmp_path: Path,
    with_issues: list[Trace],
    running_agent: tuple[str, Path, subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An SDK that leaves the analysis to the agent (Go) sends traces without issues.
    addr, directory, process = running_agent
    python_store = tmp_path / "python" / f"traces.v{STORE_VERSION}.sqlite3"
    Store(python_store).write(with_issues)
    monkeypatch.setattr(agent, "ANALYZED", False)

    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=addr))
    for trace in with_issues:
        writer.submit(dataclasses.replace(trace, issues=[]))
    writer.flush()
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)

    expected = rows(python_store)
    assert {row[2] for row in expected["issues"]} == {
        "n+1",
        "duplicates",
        "slow-sql",
        "long-transaction",
        "in-transaction",
        "error",
        "htmx",
    }
    assert rows(directory / f"traces.v{STORE_VERSION}.sqlite3") == expected


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


@pytest.fixture
def agent_console(tmp_path: Path) -> Generator[tuple[str, str, Path, subprocess.Popen[bytes]], None, None]:
    """An agent that also serves the console: (its socket address, the console's URL, its store dir, the process)."""
    directory = tmp_path / "served"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(  # noqa: S603 (the agent under test)
        [AGENT_BIN, "--dir", directory, "--serve", f"127.0.0.1:{port}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    addr_file = directory / agent.ADDR_FILE
    deadline = time.monotonic() + 10
    while not addr_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not addr_file.exists():
        process.kill()
        pytest.fail(f"agent did not start: {process.communicate()[1].decode()}")
    yield addr_file.read_text().strip(), f"http://127.0.0.1:{port}/", directory, process
    if process.poll() is None:
        process.kill()


def get(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 (the agent under test)
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def comparable(path: str, answer: tuple[int, object]) -> tuple[int, object]:
    """An issue's events over time, and the overview's, end now (milliseconds apart in the two): the counts compare."""
    status, body = answer
    now_dependent = path.startswith(("issues/detail", "traces/overview"))
    if now_dependent and isinstance(body, dict) and isinstance(body.get("series"), list):
        body = {**body, "series": sum(bucket["calls"] for bucket in body["series"])}
    return status, body


def listed(url: str) -> int:
    _, body = get(url)
    traces = body.get("traces") if isinstance(body, dict) else None
    return len(traces) if isinstance(traces, list) else 0


def test_the_agents_console_answers_as_pythons(
    client: Client,
    requests: list[Trace],
    traces: list[Trace],
    store: Store,
    agent_console: tuple[str, str, Path, subprocess.Popen[bytes]],
) -> None:
    # The same traces in tracepanel-python's store and in the agent's: the same answers from both consoles.
    addr, console, _, process = agent_console
    # Groups of every kind: a job and, when Strawberry is there, a GraphQL operation.
    if importlib.util.find_spec("strawberry"):
        client.post("/graphql/", {"query": "query Team { users { username } }"}, content_type="application/json")
    with start_trace("command", "manage.py seed", {"command.name": "seed"}, root_kind="command") as job:
        record_span(
            "sql",
            "SELECT auth_user",
            start=job.start,
            duration=40,
            attrs={
                "db.fingerprint": "SELECT * FROM auth_user",
                "db.params_hash": "a",
                "db.alias": "default",
            },
        )
    job.issues = analyze(job)
    requests = [*requests, *[t for t in traces if t not in requests], job]
    store.max_traces = 100
    store.write(requests)
    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=addr))
    for trace in requests:
        writer.submit(trace)
    writer.flush()
    ids = ",".join(trace.id for trace in requests)
    deadline = time.monotonic() + 10
    while listed(f"{console}api/traces?ids={ids}&limit=500") < len(requests):
        assert time.monotonic() < deadline, "the agent didn't write the traces"
        time.sleep(0.05)
    # Something to compare: the request that queued a task has it among its related traces.
    assert any(store.related(trace.id)["enqueued"] for trace in requests)
    assert store.summary("kind:command") is not None
    if importlib.util.find_spec("strawberry"):
        assert store.graphql_detail("query Team") is not None

    paths = [
        f"traces?ids={ids}",
        f"traces?ids={ids}&limit=2",
        "counts",
        "traces/" + "0" * 26,
        "nope",
        *(f"traces/{trace.id}" for trace in requests),
        *(f"traces/{trace.id}/related" for trace in requests),
        "search",
        # Searching traces: every kind of filter, pages, and the messages of queries the user can fix.
        *(
            f"traces?q={quote(query)}"
            for query in (
                "",
                "status>=500",
                "status:200,404",
                "status!=200",
                "has:n+1",
                "has:error",
                "has:logs",
                "route:/n-*",
                "route!=/n-*",
                "method:get",
                "duration>1ms",
                "duration<=5s",
                "queries>3",
                "kind:http,task",
                "kind:task state:success",
                "task:send_*",
                "since:1h",
                "until:2000-01-01",
                "db:default",
                "cache:default",
                f"trace:{requests[0].id}",
                "plus one",
                '"n-plus"',
                "colour:red",
                "status:abc",
                "has:everything",
                "has>1",
                "route>/a",
                "since:yesterday",
                "since>1h",
            )
        ),
        # Logs and SQL statements.
        *(
            f"logs?q={quote(query)}"
            for query in (
                "",
                "level>=warning",
                "level:error",
                "logger:tests.*",
                "hello",
                "extra.order_id:42",
                "level:loud",
            )
        ),
        "logs?limit=1",
        *(f"queries?q={quote(query)}" for query in ("", "table:auth_user", "db:default", "SELECT", "colour:red")),
        "queries?limit=2",
        # Emails, issues, the service map and the menus' values.
        *(
            f"mails?q={quote(query)}"
            for query in ("", "to:alice@example.com", "to:alice@*", "from:x@y.z", "Welcome", "subject:x")
        ),
        *(
            f"issues?q={quote(query)}"
            for query in (
                "",
                "kind:error",
                "kind!=error",
                "kind:n+1,duplicates",
                "route:/n-*",
                "SELECT",
                "since:1h",
                "colour:red",
            )
        ),
        "issues/detail?fingerprint=0000000000000000",
        "issues/detail",
        *(f"issues/detail?fingerprint={issue['fingerprint']}" for issue in store.list_issues(limit=100)),
        *(f"services?q={quote(query)}" for query in ("", "kind:database", "role:web", "default", "colour:red")),
        "values",
        # Groups of traces: lists, details, summaries and the overview.
        *(
            f"endpoints?q={quote(query)}"
            for query in ("", "method:get", "route:/n-*", "plus", "since:1h", "colour:red")
        ),
        "endpoints?limit=1",
        "endpoints/detail?method=GET&route=/n-plus-one/",
        "endpoints/detail?method=get&route=/n-plus-one/",
        "endpoints/detail?route=/nope/",
        *(f"tasks?q={quote(query)}" for query in ("", "task:send_*", "state:success", "queue:x", "colour:red")),
        *(
            f"jobs?q={quote(query)}"
            for query in ("", "source:custom", "source:manage.py", "job:manage.py *", "colour:red")
        ),
        *(f"websockets?q={quote(query)}" for query in ("", "event:connect", "colour:red")),
        *(f"graphql?q={quote(query)}" for query in ("", "type:query", "Team", "colour:red")),
        "graphql/detail?operation=" + quote("query Team"),
        "graphql/detail?operation=nope",
        *(f"traces/summary?q={quote(query)}" for query in ("kind:http", "kind:task", "kind:websocket", "colour:red")),
        *(
            f"traces/overview?q={quote(query)}"
            for query in ("", "kind:http", "since:1h", "since:2000-01-01 until:2000-01-02", "colour:red")
        ),
        "traces/overview?group=endpoint",
        "traces/overview?group=nope",
        "traces?limit=3",
        "traces?limit=3&before=99999999999999999",
        "traces?limit=oops",
    ]
    for path in paths:
        python = client.get(f"/__tracepanel__/api/{path}")
        assert comparable(path, get(console + "api/" + path)) == comparable(
            path, (python.status_code, python.json())
        ), path

    # Clearing: both delete the same traces.
    python_cleared = client.delete("/__tracepanel__/api/store")
    request = urllib.request.Request(console + "api/store", method="DELETE")  # noqa: S310 (the agent under test)
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        assert (response.status, json.loads(response.read())) == (python_cleared.status_code, python_cleared.json())
    assert get(console + "api/counts")[1] == client.get("/__tracepanel__/api/counts").json()
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def test_the_apps_console_answers_from_the_agent(
    agent_console: tuple[str, str, Path, subprocess.Popen[bytes]],
    client: Client,
    users: list[User],
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, directory, process = agent_console
    monkeypatch.setenv("TRACEPANEL_AGENT", "auto")
    monkeypatch.setenv("TRACEPANEL_DIR", str(directory))
    conf.reset()
    # A trace only the agent's store has: this process's own store stays empty.
    with start_trace("http", "GET /in-the-agent/") as trace:
        pass
    sock = agent.connect(agent.address(conf.get_config()) or "", 100)
    assert sock is not None
    with closing(sock):
        data = json.dumps([trace.to_dict()]).encode()
        sock.sendall(len(data).to_bytes(4, "big") + data)

    response = client.get(f"/__tracepanel__/api/traces/{trace.id}")

    assert (response.status_code, response.json()["name"]) == (200, "GET /in-the-agent/")

    # What only the app can do stays in the app.
    assert client.get("/__tracepanel__/api/users/me").status_code == 200

    # The agent gone: the app's console answers from its own store.
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)

    assert client.get(f"/__tracepanel__/api/traces/{trace.id}").status_code == 404
