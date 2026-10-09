import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.agentstore import AgentStore
from tracepanel.core.context import start_trace

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


def test_without_run_nothing_is_traced(tmp_path: Path) -> None:
    with_import = APP.replace("from tracepanel.storage", "import tracepanel.fastapi\nfrom tracepanel.storage")
    (tmp_path / "main.py").write_text(with_import)

    result = subprocess.run([sys.executable, "main.py"], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert json.loads(result.stdout.splitlines()[-1]) == {"header": False, "names": []}


def test_run_rejects_unknown_settings(tmp_path: Path) -> None:
    result = tracepanel("run", "--set", "slow_sql=5", "true", cwd=tmp_path)

    assert (result.returncode, result.stderr.strip()) == (
        1,
        "tracepanel run: unknown setting 'slow_sql' (see `tracepanel config`)",
    )


def test_config_shows_where_values_come_from(tmp_path: Path) -> None:
    (tmp_path / "tracepanel.toml").write_text("slow_sql_ms = 250\n")

    result = tracepanel(
        "config", cwd=tmp_path, env={"TRACEPANEL_CAPTURE_LOCALS": "0", "TRACEPANEL_CELERY_ENABLED": "0"}
    )
    settings, integrations = result.stdout.split("\nintegrations\n")
    rows = {line.split()[0]: line.split()[1:] for line in settings.splitlines()}
    states = dict(line.strip().split("  ", 1) for line in integrations.splitlines())

    assert rows["slow_sql_ms"] == ["250", "tracepanel.toml"]
    assert rows["capture_locals"] == ["false", "env", "TRACEPANEL_CAPTURE_LOCALS"]
    assert rows["widget"] == ["true", "default"]

    assert {label: state.strip() for label, state in states.items() if label in {"logging", "Celery"}} == {
        "logging": "on",
        "Celery": "off: celery_enabled",
    }


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
    messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
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
