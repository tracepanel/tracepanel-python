"""The agent's requests down an app's connection: EXPLAIN asked at a console the app doesn't serve.

A JavaScript app's console in front of a Python one (a BFF before a FastAPI app): the console asks the agent, the
agent asks the app of the trace's service down the connection its traces go up, and the app answers there.
"""

import dataclasses
import json
import os
import socket
import sqlite3
import struct
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from dirty_equals import IsInt, IsList, IsPartialDict

from tests.agentstore import AgentStore
from tests.integrations.dbapi.conftest import statements, traced
from tests.storage.test_fork import traced as traced_task
from tests.storage.test_fork import wait_for_exit
from tracepanel.console import app
from tracepanel.core import conf
from tracepanel.core import explain as plans
from tracepanel.core.model import Scalar
from tracepanel.integrations import dbapi, sqlalchemy
from tracepanel.storage import agent

# What the agent sends for a console's EXPLAIN of a SQLite query (tracepanel's crates/server/src/explain.rs).
REQUEST: dict[str, object] = {
    "explain": {
        "sql": "EXPLAIN QUERY PLAN SELECT name FROM items WHERE name = ?",
        "params": ["a"],
        "vendor": "sqlite",
        "alias": "shop",
        "server": None,
        "port": None,
        "database": "shop.sqlite3",
        "analyze": False,
    },
    "id": 7,
}


def frame(payload: object) -> bytes:
    data = json.dumps(payload).encode()
    return struct.pack(">I", len(data)) + data


class Agent:
    """An agent's socket that asks each app connecting one request, right with its answer to the hello.

    Each connection's hello and the frames that came up it, decoded; `close` ends the first connection once it is
    answered (the agent restarted).
    """

    def __init__(self, request: dict[str, object], *, close: bool = False) -> None:
        self.request, self.close = request, close
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.connections: list[list[object]] = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def address(self) -> str:
        host, port = self.listener.getsockname()
        return f"tcp:{host}:{port}"

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        received: list[object] = []
        self.connections.append(received)
        with connection, connection.makefile("rb") as stream:
            received.append(json.loads(stream.readline()))
            # In one write: the app reads its hello's answer without taking the request's bytes along.
            connection.sendall(b'{"ok": true}\n' + frame(self.request))
            while len(header := stream.read(4)) == 4:
                received.append(json.loads(stream.read(struct.unpack(">I", header)[0])))
                if self.close and received is self.connections[0]:
                    return

    def wait_for(self, frames: int, timeout: float = 10.0) -> None:
        """Until the connections together have this many frames past their hellos."""
        deadline = time.monotonic() + timeout
        while sum(len(received) - 1 for received in self.connections) < frames and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.fixture
def items(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    return path


@pytest.fixture
def answering(items: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The app answers `explain`, on the database `shop`, as its console would; it answers nothing at a console."""

    def run(statement: str, params: plans.Values, alias: str, *, analyze: bool = False) -> plans.Plan:
        def execute(sql: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
            with sqlite3.connect(items) as connection:
                return plans.cursor_rows(connection.cursor(), sql, values, session)

        return plans.explain(statement, params, vendor="sqlite", alias=alias, execute=execute, analyze=analyze)

    monkeypatch.setattr(agent, "ANSWERS", {"explain": app.explain_answer(run)})
    monkeypatch.setattr(agent, "CONSOLE", {})
    monkeypatch.setattr(agent, "SENDS", set())


def asked(request: dict[str, object], *, close: bool = False) -> Generator[Agent, None, None]:
    listening = Agent(request, close=close)
    yield listening
    listening.listener.close()


@pytest.fixture
def agent_asking() -> Generator[Agent, None, None]:
    yield from asked(REQUEST)


def writer_to(listening: Agent) -> agent.AgentWriter:
    # Nothing else in the hello's capabilities.
    config = dataclasses.replace(
        conf.get_config(), agent=listening.address, capture_request=False, profile_enabled=False
    )
    return agent.AgentWriter(config)


def test_the_agent_asks_and_the_app_answers_with_the_plan(agent_asking: Agent, answering: None) -> None:
    writer = writer_to(agent_asking)

    assert writer.connected()

    agent_asking.wait_for(1)
    ((hello, answer),) = agent_asking.connections

    assert hello == IsPartialDict(capabilities=["explain"])
    assert isinstance(hello, dict)
    assert "console" not in hello

    # The plan as the app's console has it, and the rows the agent reads its own from.
    assert answer == {
        "answer": 7,
        "vendor": "sqlite",
        "alias": "shop",
        "analyzed": False,
        "lines": [{"depth": 0, "text": "SCAN items", "warning": "Full scan of items: no index is used"}],
        "columns": ["id", "parent", "notused", "detail"],
        "rows": [[IsInt, 0, IsInt, "SCAN items"]],
    }


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("EXPLAIN UPDATE items SET name = ?", "Only SELECT statements can be explained."),
        ("EXPLAIN QUERY PLAN SELECT name FROM missing", "no such table: missing"),
    ],
)
def test_what_cant_be_explained_is_answered_with_why(answering: None, sql: str, error: str) -> None:
    for listening in asked({**REQUEST, "explain": {**REQUEST["explain"], "sql": sql}}):  # type: ignore[dict-item]
        writer_to(listening).connected()
        listening.wait_for(1)

        assert listening.connections == [[IsPartialDict(protocol=agent.PROTOCOL), {"answer": 7, "error": error}]]


def test_an_answerer_that_fails_answers_its_error(agent_asking: Agent, monkeypatch: pytest.MonkeyPatch) -> None:
    answers: dict[str, agent.Answerer] = {"explain": lambda _request: {"ratio": 1 / 0}}
    monkeypatch.setattr(agent, "ANSWERS", answers)

    writer_to(agent_asking).connected()
    agent_asking.wait_for(1)

    assert agent_asking.connections == [
        [IsPartialDict(protocol=agent.PROTOCOL), {"answer": 7, "error": "division by zero"}]
    ]


def test_traces_go_up_the_connection_the_requests_come_down(agent_asking: Agent, answering: None) -> None:
    writer = writer_to(agent_asking)
    trace = traced_task("after the answer")

    writer.connected()
    agent_asking.wait_for(1)
    writer.submit(trace)
    writer.flush()
    agent_asking.wait_for(2)

    assert agent_asking.connections == [
        [
            IsPartialDict(protocol=agent.PROTOCOL),
            IsPartialDict(answer=7),
            [IsPartialDict(id=trace.id, name="after the answer")],
        ]
    ]


def test_after_the_agent_closed_the_connection_the_next_write_connects_again(answering: None) -> None:
    for listening in asked(REQUEST, close=True):
        writer = writer_to(listening)
        trace = traced_task("after the restart")
        writer.connected()
        listening.wait_for(1)
        # The connection's reader sees the agent go.
        deadline = time.monotonic() + 5
        while writer._sock is not None and time.monotonic() < deadline:  # noqa: SLF001
            time.sleep(0.01)

        writer.submit(trace)
        writer.flush()
        listening.wait_for(3)

        first, second = listening.connections

        assert first == [IsPartialDict(protocol=agent.PROTOCOL), IsPartialDict(answer=7)]

        # The new connection is asked too: its answer and the trace go up it in whichever order.
        assert second[0] == IsPartialDict(protocol=agent.PROTOCOL)
        assert second[1:] == IsList(IsPartialDict(answer=7), [IsPartialDict(id=trace.id)], check_order=False)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork on this platform")
@pytest.mark.filterwarnings("ignore:.*multi-threaded.*fork:DeprecationWarning")
def test_a_forked_child_answers_on_its_own_connection(agent_asking: Agent, monkeypatch: pytest.MonkeyPatch) -> None:
    answers: dict[str, agent.Answerer] = {"explain": lambda _request: {"pid": os.getpid()}}
    monkeypatch.setattr(agent, "ANSWERS", answers)
    writer = writer_to(agent_asking)
    writer.connected()
    agent_asking.wait_for(1)
    done, finish = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.close(finish)
            code = 0 if writer.connected() else 2
            # Until the parent saw the answer.
            os.read(done, 1)
        finally:
            os._exit(code)
    os.close(done)
    agent_asking.wait_for(2)
    os.write(finish, b".")
    os.close(finish)
    exit_code = wait_for_exit(pid, timeout=15)

    assert exit_code == 0

    assert agent_asking.connections == [
        [IsPartialDict(protocol=agent.PROTOCOL), {"answer": 7, "pid": os.getpid()}],
        [IsPartialDict(protocol=agent.PROTOCOL), {"answer": 7, "pid": pid}],
    ]


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        (
            {"sql": "EXPLAIN SELECT * FROM t WHERE a = %(a)s AND b = %(b)s OR a = %(a)s", "params": [1, "x"]},
            ("SELECT * FROM t WHERE a = %(a)s AND b = %(b)s OR a = %(a)s", {"a": 1, "b": "x"}, "default", False),
        ),
        (
            {"sql": "EXPLAIN SELECT * FROM t WHERE a = :a AND b::text = ':c'", "params": [1], "alias": "oracle:shop"},
            ("SELECT * FROM t WHERE a = :a AND b::text = ':c'", {"a": 1}, "oracle:shop", False),
        ),
        (
            {"sql": "EXPLAIN SELECT * FROM t WHERE a = %(a)s", "params": [1], "param_names": ["z"]},
            ("SELECT * FROM t WHERE a = %(a)s", {"z": 1}, "default", False),
        ),
        (
            {"sql": "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM t WHERE a = $1", "params": [1], "analyze": True},
            ("SELECT * FROM t WHERE a = $1", [1], "default", True),
        ),
        (
            {"sql": "EXPLAIN ANALYZE SELECT * FROM t WHERE a = :1", "params": [1]},
            ("SELECT * FROM t WHERE a = :1", [1], "default", False),
        ),
        ({"sql": "EXPLAIN SELECT 1", "params": []}, ("SELECT 1", [], "default", False)),
    ],
)
def test_the_request_is_what_the_console_would_run(
    request_: dict[str, object], expected: tuple[str, list[Scalar] | dict[str, Scalar], str, bool]
) -> None:
    # The captured statement, its parameters as the cursor took them (named ones by their names).
    assert plans.asked(request_) == expected


def test_the_agent_explains_a_query_of_the_app_on_the_apps_database(
    store: AgentStore, items: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As the app sets it up (tracepanel._setup): a driver's database, else SQLAlchemy's; nothing at a console.
    monkeypatch.setattr(agent, "ANSWERS", {"explain": app.explain_answer(dbapi.explain_or(sqlalchemy.explain))})
    monkeypatch.setattr(agent, "CONSOLE", {})
    connection = sqlite3.connect(items)
    trace = traced(lambda: connection.execute("SELECT name FROM items WHERE name = ?", ("a",)))
    connection.close()
    (span,) = statements(trace)
    service = str(trace.attrs["service.name"])
    writer = agent.AgentWriter(dataclasses.replace(store.config(), service=service))

    writer.submit(trace)
    writer.flush()
    store.wait_for(trace.id)

    assert store.get("capabilities", service=service) == IsPartialDict(explain=True)

    assert store.get("explain", trace=trace.id, span=span.id) == {
        "vendor": "sqlite",
        "alias": "sqlite3:shop.sqlite3",
        "analyzed": False,
        "lines": [{"depth": 0, "text": "SCAN items", "warning": "Full scan of items: no index is used"}],
    }
