"""Lines in the terminal: one as a server starts, one after each task run and management command.

    tracepanel ▸ on (DEBUG) · Django · logging, SQLAlchemy, Celery, DRF → console at /__tracepanel__/
    tracepanel ▸ task blog.tasks.send_digest · success · 230 ms · 14 queries → http://127.0.0.1:52811/#/t/01M4…

The first says that tracepanel is on, why, and what it traces; the others link to the trace (and its issues, which
the agent finds) in the agent's console, where there is no page for the widget. Turned off with `terminal_summary`.
"""

import os
import sys
from typing import TextIO

from tracepanel.core import conf, registry
from tracepanel.core.model import Trace
from tracepanel.storage import agent, transport

KINDS = frozenset({"task", "command"})


# Longer than this reads better in seconds.
SECONDS_FROM_US = 10_000_000


def _duration(us: int) -> str:
    return f"{us / 1000:.0f} ms" if us < SECONDS_FROM_US else f"{us / 1_000_000:.1f} s"


def line(trace: Trace, console_url: str) -> str:
    sql = sum(1 for span in trace.spans if span.kind == "sql")
    state = trace.attrs.get("task.state")
    parts = [
        f"{trace.kind} {trace.name.removeprefix('manage.py ')}",
        *([str(state).lower()] if state else ["failed"] if trace.status == "error" else []),
        _duration(trace.duration),
        f"{sql} {'query' if sql == 1 else 'queries'}",
    ]
    return f"tracepanel ▸ {' · '.join(parts)} → {console_url.rstrip('/')}/#/t/{trace.id}"


def summarize(trace: Trace, out: TextIO | None = None) -> None:
    config = conf.get_config()
    if trace.kind not in KINDS or not config.terminal_summary:
        return
    stream = out or sys.stderr
    # The agent's console when it serves this store (on a port of its own; the first command may start it).
    served = agent.console_url(config) if agent.ensure_started(config) else None
    stream.write(line(trace, served or config.console_url) + "\n")
    stream.flush()


# Set once the line is out: a reloader's processes (runserver, flask run --debug) inherit it and stay quiet.
ANNOUNCED = "TRACEPANEL_ANNOUNCED"


def announce(framework: str, *, debug: str = "debug mode", out: TextIO | None = None) -> None:
    """The line a server process starts with: tracepanel is on, why, the framework and libraries it traces."""
    config = conf.get_config()
    if not (config.enabled and config.terminal_summary) or os.environ.get(ANNOUNCED) or "pytest" in sys.modules:
        return
    os.environ[ANNOUNCED] = "1"
    origin = next(origin for name, _, origin in conf.explain() if name == "enabled")
    # The framework's own switch (DEBUG, debug mode), or the setting that overrode it.
    why = debug if origin == "framework settings" else origin.removeprefix("env ")
    libraries = ", ".join(integration.label for integration in registry.active(django=framework == "Django"))
    parts = [f"on ({why})", framework, *([libraries] if libraries else [])]
    stream = out or sys.stderr
    stream.write(f"tracepanel ▸ {' · '.join(parts)} → console at {config.prefix}\n")
    stream.flush()


def install() -> None:
    transport.on_complete(summarize)
