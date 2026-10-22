"""Sending traces to tracepanel-agent: over a unix socket or TCP, and what happens without one.

The agent under test is the one uv built from ../tracepanel (or $TRACEPANEL_AGENT_BIN).
"""

import dataclasses
import json
import platform
import signal
import socket
import struct
import subprocess
import threading
import time
from importlib import metadata
from pathlib import Path

import django
import pytest
import yaml
from dirty_equals import IsList, IsPartialDict, IsPositiveInt, IsStr
from django.contrib.auth.models import User
from django.test import Client

from tests.agentstore import AGENT_SCHEMAS, AgentStore, newer
from tracepanel.console import proxy
from tracepanel.core import appinfo, conf, runtime
from tracepanel.core.context import record_span, start_trace
from tracepanel.core.model import Trace
from tracepanel.storage import agent, transport
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


def test_processes_starting_the_agent_together_start_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An app's workers, forked together, each start the store's agent: one does, the others find it answering.
    directory = tmp_path / ".tracepanel"
    config = dataclasses.replace(conf.get_config(), agent="auto", dir=str(directory))
    spawned: list[subprocess.Popen[bytes] | None] = []
    spawn = agent._spawn  # noqa: SLF001 (what start() does once no agent answers)

    def recorded(executable: str, directory: Path, config: conf.Config) -> subprocess.Popen[bytes] | None:
        spawned.append(process := spawn(executable, directory, config))
        return process

    monkeypatch.setattr(agent, "_spawn", recorded)
    workers = 8
    barrier = threading.Barrier(workers)
    started: list[subprocess.Popen[bytes] | None] = []

    def worker() -> None:
        barrier.wait()
        started.append(agent.start(config))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        (process,) = spawned
        assert [process for process in started if process is not None] == [process]
        assert process is not None
        assert process.poll() is None

        assert (agent.answers(agent.address(config)), agent.console_url(config)) == (
            True,
            IsStr(regex=r"http://127\.0\.0\.1:\d+/"),
        )

        assert capsys.readouterr().err == f"tracepanel ▸ agent started for {directory}\n"
    finally:
        for process in spawned:
            if process is not None:
                process.terminate()
                process.wait(timeout=10)


def test_the_first_trace_after_the_agent_restarted_reaches_the_new_one(tmp_path: Path) -> None:
    directory = tmp_path / ".tracepanel"
    first = AgentStore.start(directory)
    config = dataclasses.replace(conf.get_config(), agent="auto", dir=str(directory))
    writer = agent.AgentWriter(config)
    before, after = requests(2)
    writer.submit(before)
    writer.flush()
    first.wait_for(before.id)
    # The agent goes (a rebuild, a restart): the writer's connection to it is still open on this side.
    first.stop()
    second = AgentStore.start(directory)
    try:
        writer.submit(after)
        writer.flush()

        second.wait_for(after.id)
    finally:
        second.stop()


def _hello(tmp_path: Path, config: conf.Config) -> dict[str, object]:
    """The hello an app's writer sends, as an agent listening at a socket of the test's reads it."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    heard: list[bytes] = []

    def answer() -> None:
        connection, _ = listener.accept()
        with connection:
            heard.append(connection.makefile("rb").readline())
            connection.sendall(b'{"ok": true}\n')

    thread = threading.Thread(target=answer)
    thread.start()
    host, port = listener.getsockname()
    writer = agent.AgentWriter(dataclasses.replace(config, agent=f"tcp:{host}:{port}", dir=str(tmp_path)))
    writer.submit(requests(1)[0])
    writer.flush()
    thread.join(5)
    listener.close()
    return json.loads(heard[0])


def test_the_hello_says_what_the_app_offers_the_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # EXPLAIN answered at the app's console; switching users offered, but off here.
    monkeypatch.setattr(agent, "CONSOLE", {"explain": lambda: True, "users": lambda: False})
    # Celery's task events.
    monkeypatch.setattr(agent, "SENDS", {"live_tasks"})
    config = dataclasses.replace(conf.get_config(), capture_request=True, profile_enabled=True, service="shop")

    hello = _hello(tmp_path, config)

    assert hello == IsPartialDict(
        {
            "protocol": agent.PROTOCOL,
            "service": "shop",
            "capabilities": ["profile", "request", "live_tasks"],
            "console": ["explain"],
        }
    )


def test_an_app_that_offers_nothing_says_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "CONSOLE", {})
    monkeypatch.setattr(agent, "SENDS", set())
    config = dataclasses.replace(conf.get_config(), capture_request=False, profile_enabled=False)

    hello = _hello(tmp_path, config)

    assert "capabilities" not in hello
    assert "console" not in hello


@pytest.fixture
def django_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process as a Django app: another framework's tests may have set up theirs."""
    monkeypatch.setattr(runtime, "_framework", None)
    monkeypatch.setattr(runtime, "_named", None)
    runtime.set_framework("Django")


def test_the_hello_carries_the_apps_settings_masked_and_its_versions(tmp_path: Path, django_app: None) -> None:
    hello = _hello(tmp_path, conf.get_config())
    app = hello["app"]
    assert isinstance(app, dict)

    assert app["settings"] == IsPartialDict(
        {
            "DEBUG": "True",
            "SECRET_KEY": "********",
            "ROOT_URLCONF": '"tests.project.urls"',
            "INSTALLED_APPS": IsStr(regex=r'\["django.contrib.auth", .*"tracepanel.django".*\]'),
            "DATABASES": IsStr(regex=r'\{"analytics": \{.*"default": \{.*\}\}'),
            "PASSWORD_HASHERS": IsStr(regex=r'\["django.contrib.auth.hashers.PBKDF2PasswordHasher", .*\]'),
        }
    )
    assert all(name.isupper() for name in app["settings"])
    assert list(app["settings"]) == sorted(app["settings"])

    assert app["versions"] == IsList(
        {"name": "Python", "version": platform.python_version(), "kind": "runtime"},
        {"name": "Django", "version": django.get_version(), "kind": "framework"},
        {"name": "tracepanel", "version": metadata.version("tracepanel"), "kind": "package"},
        check_order=False,
        length=...,
    )
    assert app["versions"][:2] == [
        {"name": "Python", "version": platform.python_version(), "kind": "runtime"},
        {"name": "Django", "version": django.get_version(), "kind": "framework"},
    ]
    packages = app["versions"][2:]
    assert [package["name"].lower() for package in packages] == sorted(package["name"].lower() for package in packages)
    assert {package["kind"] for package in packages} == {"package"}
    assert "django" not in {package["name"].lower() for package in packages}


def test_with_capture_settings_off_the_hello_carries_the_versions_only(tmp_path: Path, django_app: None) -> None:
    config = dataclasses.replace(conf.get_config(), capture_settings=False)

    hello = _hello(tmp_path, config)

    assert hello["app"] == {"versions": IsList(IsPartialDict({"kind": "runtime"}), length=...)}


def test_the_agent_serves_what_the_hello_said_of_the_app(store: AgentStore, django_app: None) -> None:
    if "/app" not in yaml.safe_load((AGENT_SCHEMAS / "openapi.yaml").read_text())["paths"]:
        pytest.skip("the released agent doesn't serve api/app: a newer one does")
    config = dataclasses.replace(store.config(), service="shop")
    writer = agent.AgentWriter(config)

    assert writer.connected()

    answer = store.get("app", service="shop")

    # No checks here (the tests add them where they test them): an agent that serves them has nulls.
    assert answer == newer(
        {
            "service": "shop",
            "agent": IsStr(),
            "connected": True,
            "received": IsPositiveInt,
            "settings": IsPartialDict({"DEBUG": "True", "SECRET_KEY": "********"}),
            "versions": appinfo.describe(config)["versions"],
        },
        answer,
        {"checks": None, "migrations": None},
    )


FOUND: dict[str, object] = {
    "checks": [
        {
            "id": "models.W042",
            "level": "warning",
            "message": "Auto-created primary key used",
            "hint": None,
            "object": "shop.Order",
        }
    ],
    "migrations": {"unapplied": [{"app": "shop", "names": ["0002_order_note"]}], "changes": []},
}


def test_the_checks_run_off_the_connection_and_the_agent_connected_then_is_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Slow checks (a database that takes its time): the hello doesn't wait for them.
    release = threading.Event()

    def slow() -> dict[str, object]:
        release.wait(5)
        return FOUND

    appinfo.add_checks(slow)
    appinfo.add_checks(lambda: {"checks": 1 / 0})  # a check that fails says nothing
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    heard: list[object] = []

    def agent_side() -> None:
        connection, _ = listener.accept()
        with connection:
            reader = connection.makefile("rb")
            heard.append(json.loads(reader.readline()))
            connection.sendall(b'{"ok": true}\n')
            release.set()
            length = struct.unpack(">I", reader.read(4))[0]
            heard.append(json.loads(reader.read(length)))

    thread = threading.Thread(target=agent_side)
    thread.start()
    host, port = listener.getsockname()
    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=f"tcp:{host}:{port}", dir=str(tmp_path)))
    monkeypatch.setattr(transport, "_writer", writer)

    assert writer.connected()

    thread.join(5)
    listener.close()
    hello, frame = heard

    assert isinstance(hello, dict)
    assert set(hello["app"]) == {"settings", "versions"}

    assert frame == {"app": FOUND}

    # A connection after it says them in its hello.
    assert appinfo.describe(conf.get_config()) == IsPartialDict(FOUND)


def test_the_agent_serves_what_the_checks_found(
    store: AgentStore, django_app: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = yaml.safe_load((AGENT_SCHEMAS / "openapi.yaml").read_text())["components"]["schemas"]["App"]
    if "checks" not in schema["properties"]:
        pytest.skip("the released agent doesn't keep an app's checks: a newer one does")
    release = threading.Event()

    def slow() -> dict[str, object]:
        release.wait(5)
        return FOUND

    appinfo.add_checks(slow)
    writer = agent.AgentWriter(dataclasses.replace(store.config(), service="shop"))
    monkeypatch.setattr(transport, "_writer", writer)

    assert writer.connected()

    before = store.get("app", service="shop")
    release.set()
    deadline = time.monotonic() + 5
    while (after := store.get("app", service="shop"))["checks"] is None and time.monotonic() < deadline:
        time.sleep(0.02)

    assert before == IsPartialDict({"checks": None, "migrations": None})
    assert after == IsPartialDict({"connected": True, **FOUND})
