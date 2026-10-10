import io
import os
import sys

import pytest
from dirty_equals import IsStr

from tracepanel.console import terminal
from tracepanel.core import conf
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Span, Trace


def task(state: str | None = None) -> Trace:
    attrs: Attrs = {"task.state": state} if state else {}
    trace = Trace(id=new_id(), kind="task", name="blog.tasks.digest", start=0, duration=230_000, attrs=attrs)
    trace.spans += [Span(id=new_id(), parent_id=None, kind="sql", name="SELECT", start=0) for _ in range(14)]
    return trace


def test_line_links_to_the_console() -> None:
    trace = task("SUCCESS")

    # Its issues are the agent's, found once it is stored: the link leads to them.
    assert terminal.line(trace, "http://127.0.0.1:8765/") == (
        f"tracepanel ▸ task blog.tasks.digest · success · 230 ms · 14 queries → http://127.0.0.1:8765/#/t/{trace.id}"
    )


def test_commands_and_requests() -> None:
    command = Trace(id=new_id(), kind="command", name="manage.py migrate", start=0, duration=5000, status="error")
    out = io.StringIO()

    terminal.summarize(command, out)
    terminal.summarize(Trace(id=new_id(), kind="http", name="GET /", start=0), out)

    link = f"http://127.0.0.1:8765/#/t/{command.id}"
    assert out.getvalue() == f"tracepanel ▸ command migrate · failed · 5 ms · 0 queries → {link}\n"


def test_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_TERMINAL_SUMMARY", "0")
    conf.reset()
    out = io.StringIO()

    terminal.summarize(task(), out)

    assert out.getvalue() == ""


@pytest.fixture
def announcing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The line is for servers: not under pytest, and once per run.
    monkeypatch.delitem(sys.modules, "pytest")
    # Set then removed through monkeypatch, so the variable announce() sets is gone again after the test.
    monkeypatch.setenv(terminal.ANNOUNCED, "")
    monkeypatch.delenv(terminal.ANNOUNCED)
    for name in [name for name in os.environ if name.startswith("TRACEPANEL_")]:
        monkeypatch.delenv(name)


def test_startup_line_says_why_and_what(announcing: None, monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    monkeypatch.setenv("TRACEPANEL_CELERY_ENABLED", "false")
    conf.reset()

    terminal.announce("Django", debug="DEBUG", out=out)
    terminal.announce("Django", debug="DEBUG", out=out)

    (line,) = out.getvalue().splitlines()
    assert line == IsStr(
        regex=r"tracepanel ▸ on \(DEBUG\) · Django · logging, SQLAlchemy, .* → console at /__tracepanel__/"
    )
    assert "Celery" not in line


def test_startup_line_names_the_override(announcing: None, monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    monkeypatch.setenv("TRACEPANEL_ENABLED", "1")
    conf.reset()

    terminal.announce("Flask", out=out)

    assert out.getvalue().startswith("tracepanel ▸ on (TRACEPANEL_ENABLED) · Flask · ")
    # Django's own integrations aren't traced in a Flask process.
    assert "DRF" not in out.getvalue()


def test_no_startup_line_when_off(announcing: None, monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    monkeypatch.setenv("TRACEPANEL_ENABLED", "0")
    conf.reset()

    terminal.announce("Flask", out=out)

    assert out.getvalue() == ""
