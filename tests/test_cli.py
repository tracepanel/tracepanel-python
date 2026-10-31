import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.agentstore import AgentStore
from tracepanel import _cli
from tracepanel.core import conf
from tracepanel.core.context import start_trace
from tracepanel.storage import agent

pytest.importorskip("fastapi")

# A FastAPI app with no tracepanel in its code: `tracepanel run` alone must trace it.
APP = """
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tracepanel.storage import transport

traces = []
class Collector:
    def submit(self, trace): traces.append(trace)
    def flush(self, timeout=5.0): pass
transport.set_writer(Collector())

app = FastAPI()

@app.get("/items/{item_id}")
def item(item_id: int) -> dict[str, int]:
    return {"id": item_id}

response = TestClient(app).get("/items/3")
print(json.dumps({"header": "tracepanel-id" in response.headers, "names": [t.name for t in traces]}))
"""


def tracepanel(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TRACEPANEL_")}
    # Not under pytest there: without "off" the first trace would start an agent that outlives the test.
    return subprocess.run(  # noqa: S603 (this interpreter, tracepanel's own CLI)
        [sys.executable, "-m", "tracepanel", *args],
        cwd=cwd,
        env={**clean, "TRACEPANEL_AGENT": "off", **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_traces_an_app_without_code_changes(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(APP)

    result = tracepanel("run", "--set", "terminal_summary=false", sys.executable, "main.py", cwd=tmp_path)

    assert json.loads(result.stdout.splitlines()[-1]) == {"header": True, "names": ["GET /items/{item_id}"]}


# Under run the app is set up as it is created, before any route is added: routers come later.
ROUTERS_APP = """
import json
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from tracepanel.storage import transport

traces = []
class Collector:
    def submit(self, trace): traces.append(trace)
    def flush(self, timeout=5.0): pass
transport.set_writer(Collector())

app = FastAPI()

@app.get("/direct")
def direct() -> int:
    return 1

items = APIRouter(prefix="/items")
nested = APIRouter(prefix="/nested")

@items.get("/{item_id}")
def item(item_id: int) -> int:
    return item_id

@nested.get("/{item_id}")
def nested_item(item_id: int) -> int:
    return item_id

items.include_router(nested)
app.include_router(items, prefix="/v1")

client = TestClient(app)
for path in ("/direct", "/v1/items/3", "/v1/items/nested/4"):
    client.get(path)
print(json.dumps([[span.kind, span.name] for trace in traces for span in trace.spans]))
"""


def test_run_runs_the_routes_of_included_routers_in_a_view(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(ROUTERS_APP)

    result = tracepanel("run", "--set", "terminal_summary=false", sys.executable, "main.py", cwd=tmp_path)

    assert json.loads(result.stdout.splitlines()[-1]) == [
        ["http", "GET /direct"],
        ["view", "__main__.direct"],
        ["http", "GET /v1/items/{item_id}"],
        ["view", "__main__.item"],
        ["http", "GET /v1/items/nested/{item_id}"],
        ["view", "__main__.nested_item"],
    ]


def test_without_run_nothing_is_traced(tmp_path: Path) -> None:
    with_import = APP.replace("from tracepanel.storage", "import tracepanel.fastapi\nfrom tracepanel.storage")
    (tmp_path / "main.py").write_text(with_import)

    result = subprocess.run([sys.executable, "main.py"], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert json.loads(result.stdout.splitlines()[-1]) == {"header": False, "names": []}


# A virtualenv of its own, as a pyenv or system Python: no tracepanel in it.
def bare_python(tmp_path: Path) -> str:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tmp_path / "bare")], check=True)  # noqa: S603
    return str(tmp_path / "bare" / "bin" / "python")


def test_run_with_an_interpreter_without_tracepanel_only_says_so(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("print('ran')\n")

    result = tracepanel("run", bare_python(tmp_path), "script.py", cwd=tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "ran\n",
        "tracepanel: not tracing this process: No module named 'tracepanel'\n",
    )


def test_run_still_runs_the_projects_sitecustomize(tmp_path: Path) -> None:
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "sitecustomize.py").write_text("import builtins\nbuiltins.customized = True\n")
    (tmp_path / "script.py").write_text("print(customized)\n")
    env = {"PYTHONPATH": str(tmp_path / "site")}

    traced = tracepanel("run", sys.executable, "script.py", cwd=tmp_path, env=env)
    bare = tracepanel("run", bare_python(tmp_path), "script.py", cwd=tmp_path, env=env)

    assert (traced.returncode, traced.stdout, traced.stderr) == (0, "True\n", "")

    assert (bare.returncode, bare.stdout, bare.stderr) == (
        0,
        "True\n",
        "tracepanel: not tracing this process: No module named 'tracepanel'\n",
    )


def test_run_rejects_unknown_settings(tmp_path: Path) -> None:
    result = tracepanel("run", "--set", "slow_sql=5", "true", cwd=tmp_path)

    assert (result.returncode, result.stderr.strip()) == (
        1,
        "tracepanel run: unknown setting 'slow_sql' (see `tracepanel config`)",
    )


def test_config_shows_where_values_come_from(tmp_path: Path) -> None:
    (tmp_path / "tracepanel.toml").write_text(
        "slow_sql_ms = 250\n[slow_sql_ms_by_db]\nsnowflake = 3000\nbigquery = 5000\n"
    )

    result = tracepanel(
        "config", cwd=tmp_path, env={"TRACEPANEL_CAPTURE_LOCALS": "0", "TRACEPANEL_CELERY_ENABLED": "0"}
    )
    settings, integrations = result.stdout.split("\nintegrations\n")
    rows = {line.split()[0]: line.split()[1:] for line in settings.splitlines()}
    states = dict(line.strip().split("  ", 1) for line in integrations.splitlines())

    assert rows["slow_sql_ms"] == ["250", "tracepanel.toml"]
    assert rows["slow_sql_ms_by_db"] == ["snowflake=3000,", "bigquery=5000", "tracepanel.toml"]
    assert rows["long_transaction_ms_by_db"] == ["default"]
    assert rows["capture_locals"] == ["false", "env", "TRACEPANEL_CAPTURE_LOCALS"]
    assert rows["widget"] == ["true", "default"]

    assert {label: state.strip() for label, state in states.items() if label in {"logging", "Celery"}} == {
        "logging": "on",
        "Celery": "off: celery_enabled",
    }


def test_the_agent_the_cli_runs_gets_the_prices_file_of_the_settings_but_not_its_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(agent, "binary", lambda: "tracepanel-agent")
    monkeypatch.setattr(os, "execv", lambda _path, argv: ran.append(argv))  # pyrefly: ignore[implicit-any-lambda]
    monkeypatch.setenv("TRACEPANEL_ROOT", str(tmp_path))
    monkeypatch.setenv("TRACEPANEL_LLM_PRICES", "prices.json")
    conf.reset()

    _cli.dev([])
    _cli.run_agent(["mcp"])

    store, prices = str(tmp_path / ".tracepanel"), str(tmp_path / "prices.json")
    assert ran == [
        ["tracepanel-agent", "--dir", store, "--llm-prices", prices, "--serve", "127.0.0.1:8765"],
        ["tracepanel-agent", "--dir", store, "mcp"],
    ]


# No framework at all (a script, a worker): SQLAlchemy is still traced, and nothing complains while patching.
SCRIPT = """
import json
from sqlalchemy import create_engine, text
from tracepanel.core.context import start_trace
from tracepanel.storage import transport

traces = []
class Collector:
    def submit(self, trace): traces.append(trace)
    def flush(self, timeout=5.0): pass
transport.set_writer(Collector())

engine = create_engine("sqlite://")
with start_trace("command", "script") as trace, engine.connect() as connection:
    connection.execute(text("SELECT 1"))
print(json.dumps([span.kind for span in trace.spans]))
"""


def test_run_traces_sqlalchemy_without_a_framework(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text(SCRIPT)

    result = tracepanel("run", "--set", "terminal_summary=false", sys.executable, "script.py", cwd=tmp_path)

    assert result.stderr == ""
    assert json.loads(result.stdout.splitlines()[-1]) == ["command", "transaction", "sql"]


# A script with no framework: the libraries it imports are traced all the same.
DRIVER_SCRIPT = """
import json
import sqlite3
from tracepanel.core.context import start_trace
from tracepanel.storage import transport

traces = []
class Collector:
    def submit(self, trace): traces.append(trace)
    def flush(self, timeout=5.0): pass
transport.set_writer(Collector())

with start_trace("command", "script") as trace:
    sqlite3.connect(":memory:").execute("SELECT 1").fetchall()
print(json.dumps([span.kind for span in trace.spans]))
"""


def test_run_traces_a_driver_without_a_framework(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text(DRIVER_SCRIPT)

    result = tracepanel("run", "--set", "terminal_summary=false", sys.executable, "script.py", cwd=tmp_path)

    assert result.stderr == ""
    assert json.loads(result.stdout.splitlines()[-1]) == ["command", "sql"]


# A FastAPI app imports tracepanel's database drivers module before the app imports sqlite3.
APP_THEN_DRIVER = """
import json
from fastapi import FastAPI
from tracepanel.core.context import start_trace
from tracepanel.storage import transport

traces = []
class Collector:
    def submit(self, trace): traces.append(trace)
    def flush(self, timeout=5.0): pass
transport.set_writer(Collector())

app = FastAPI()
import sqlite3

with start_trace("command", "script") as trace:
    sqlite3.connect(":memory:").execute("SELECT 1").fetchall()
print(json.dumps([span.kind for span in trace.spans]))
"""


def test_run_traces_a_driver_imported_after_the_app(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(APP_THEN_DRIVER)

    result = tracepanel("run", "--set", "terminal_summary=false", sys.executable, "main.py", cwd=tmp_path)

    assert result.stderr == ""
    assert json.loads(result.stdout.splitlines()[-1]) == ["command", "sql"]


def test_run_help_is_the_usage(tmp_path: Path) -> None:
    result = tracepanel("run", "--help", cwd=tmp_path)

    assert (result.returncode, result.stdout) == (0, "usage: tracepanel run [--set name=value]... <command>\n")


def test_clear_deletes_the_store_after_asking(tmp_path: Path) -> None:
    # The project's agent holds the store: clear asks it to delete.
    store = AgentStore.start(tmp_path / ".tracepanel")
    for name in ("GET /a/", "GET /b/"):
        with start_trace("http", name) as trace:
            pass
        store.write([trace])
    env = {"TRACEPANEL_ROOT": str(tmp_path), "TRACEPANEL_AGENT": "auto"}

    refused = subprocess.run(
        [sys.executable, "-m", "tracepanel", "clear"],
        cwd=tmp_path,
        env={**os.environ, **env},
        input="n\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert (refused.returncode, refused.stderr) == (1, "tracepanel clear: nothing deleted\n")

    cleared = tracepanel("clear", "--yes", cwd=tmp_path, env=env)
    store.stop()
    assert (cleared.returncode, cleared.stdout) == (0, "tracepanel: deleted 2 traces\n")


def test_mcp_answers_from_the_projects_store(tmp_path: Path) -> None:
    store = AgentStore.start(tmp_path / ".tracepanel")
    with start_trace("http", "GET /orders/") as trace:
        pass
    store.write([trace])
    store.stop()
    messages: list[dict[str, object]] = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "find_traces", "arguments": {}}},
    ]

    result = subprocess.run(
        [sys.executable, "-m", "tracepanel", "mcp"],
        cwd=tmp_path,
        env={**os.environ, "TRACEPANEL_ROOT": str(tmp_path)},
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        check=False,
    )
    replies = [json.loads(line) for line in result.stdout.splitlines()]

    assert (result.returncode, result.stderr) == (0, "")

    assert [reply["id"] for reply in replies] == [0, 1]
    assert replies[0]["result"]["serverInfo"]["name"] == "tracepanel"

    text = replies[1]["result"]["content"][0]["text"]
    assert text.startswith(f"traces (1, newest first):\n{trace.id}  http GET /orders/ · ")
