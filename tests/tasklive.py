"""Task events, as the tests see them: a writer that keeps them, and the agent's list of tasks in flight."""

import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.agentstore import AgentStore
from tracepanel.core import conf
from tracepanel.core.model import Trace

Event = dict[str, Any]


class Recorder:
    """A writer that keeps the traces and the task events, in the order they were sent."""

    def __init__(self) -> None:
        self.traces: list[Trace] = []
        self.events: list[Event] = []

    def submit(self, trace: Trace) -> None:
        self.traces.append(trace)

    def submit_event(self, event: Event) -> None:
        self.events.append(event)

    def flush(self, timeout: float = 5.0) -> None:
        pass


def setenv(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """A TRACEPANEL_* variable, read at once: an earlier test's heartbeat thread may have cached the settings since."""
    monkeypatch.setenv(name, value)
    conf.reset()


def without_heartbeats(events: list[Event]) -> list[Event]:
    # A worker says it is there when it starts, however the test starts it.
    return [event for event in events if event["event"] != "heartbeat"]


def heartbeats(events: list[Event], worker: str) -> list[Event]:
    # Of that worker: an earlier test's worker threads still beat, into whichever writer is current.
    return [event for event in events if event["event"] == "heartbeat" and event.get("worker") == worker]


def named(answer: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """The workers of that name: an earlier test's worker threads still beat, into whichever agent is current."""
    return [worker for worker in answer["workers"] if worker["name"] == name]


def live(store: AgentStore) -> dict[str, Any]:
    answer = store.call("tasks/live")
    if answer is None:
        pytest.skip("the released agent lists no tasks in flight: a newer one does")
    return answer


def live_when(store: AgentStore, ready: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        answer = live(store)
        if ready(answer):
            return answer
        time.sleep(0.02)
    msg = f"api/tasks/live never got there: {live(store)}"
    raise AssertionError(msg)
