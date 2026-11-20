"""No console answers: 503, why and what to do; a browser's page gets a page that reloads itself until it does."""

import json
import re
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from tracepanel.console import app
from tracepanel.console.messages import Request
from tracepanel.core import conf
from tracepanel.storage import agent

HTML = "text/html,application/xhtml+xml"
RETRY = {"Retry-After": "2", "Cache-Control": "no-store"}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The project's store, no agent of its own; the tests never start one (under pytest the SDK doesn't)."""
    directory = tmp_path / ".tracepanel"
    directory.mkdir()
    monkeypatch.setenv("TRACEPANEL_DIR", str(directory))
    monkeypatch.setenv("TRACEPANEL_AGENT", "auto")
    monkeypatch.setenv("TRACEPANEL_AGENT_BIN", str(tmp_path / "no-such-agent"))
    conf.reset()
    return directory


@pytest.fixture
def without_console(store: Path) -> Iterator[Path]:
    """An agent that takes traces and serves no console (started without --serve): its address in the store."""
    with socket.create_server(("127.0.0.1", 0)) as listening:
        (store / "agent.addr").write_text(f"tcp:127.0.0.1:{listening.getsockname()[1]}\n")
        yield store


def answered(accept: str = "") -> dict[str, object]:
    response = app.dispatch(Request(method="GET", path="api/counts" if not accept else "", accept=accept))
    return {
        "status": response.status,
        "type": response.content_type,
        "headers": response.headers,
        "body": response.body.decode(),
    }


def error(message: str) -> dict[str, object]:
    return {
        "status": 503,
        "type": "application/json",
        "headers": RETRY,
        "body": json.dumps({"error": message}, separators=(",", ":")),
    }


def runs_without_console(store: Path) -> str:
    return (
        "The project's agent runs without its console (it was started without --serve). Stop it: the next request "
        f"starts one with its console. Store: {store}."
    )


def test_an_agent_without_its_console_gets_a_page_that_says_to_stop_it_and_reloads(without_console: Path) -> None:
    page = answered(HTML)
    body = str(page["body"])

    assert page == {"status": 503, "type": "text/html; charset=utf-8", "headers": RETRY, "body": body}

    assert '<meta http-equiv="refresh" content="2">' in body
    assert "<h1>Tracepanel&#x27;s agent runs without its console</h1>" in body
    detail = runs_without_console(without_console).replace("'", "&#x27;")
    assert f"<p>{detail}</p>" in body


def test_an_agent_without_its_console_its_console_address_left_over(without_console: Path) -> None:
    (without_console / "console.addr").write_text("http://127.0.0.1:9/\n")

    assert answered() == error(runs_without_console(without_console))


def test_the_agent_off_says_so(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_AGENT", "off")
    conf.reset()

    assert answered() == error(
        "Tracepanel's agent is off (the agent setting, TRACEPANEL_AGENT=off): traces aren't kept and there is no "
        "console. Set it to auto to have the app start it."
    )


def test_a_named_agent_without_a_console_says_where_it_looked(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_AGENT", "tcp:127.0.0.1:9")
    monkeypatch.setenv("TRACEPANEL_CONSOLE_URL", "http://127.0.0.1:9/")
    conf.reset()

    assert answered() == error(
        "No console answers at http://127.0.0.1:9/ (the console_url setting) for the agent at tcp:127.0.0.1:9. "
        "Start that agent with --serve, or correct the address."
    )


def test_no_binary_to_start_says_how_to_get_one(store: Path) -> None:
    missing = store.parent / "no-such-agent"

    assert answered() == error(
        f"TRACEPANEL_AGENT_BIN names {missing}, which isn't there. Set it to the tracepanel-agent binary's path."
    )


def test_an_agent_that_does_not_start_points_to_its_log(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = store.parent / "tracepanel-agent"
    binary.write_text("#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("TRACEPANEL_AGENT_BIN", str(binary))

    assert answered() == error(
        f"Tracepanel's agent didn't start: {store / 'agent.log'} says why. It is tried again within 30 seconds."
    )


def test_the_page_needs_nothing_from_elsewhere_and_what_it_says_makes_no_markup() -> None:
    page = app.page(agent.NoConsole("A <b>title</b>", "Store: /tmp/\"x\" & 'y'."))

    assert "<title>Tracepanel: A &lt;b&gt;title&lt;/b&gt;</title>" in page
    assert "<p>Store: /tmp/&quot;x&quot; &amp; &#x27;y&#x27;.</p>" in page

    assert re.search(r"<(script|link|img)\b|https?:", page) is None
