"""What every task queue integration shares: the `task.*` model, independent of the queue.

A task run is a `task` trace (root span `task.run`), an enqueue a `task.enqueue` span; `task.system` says which
queue ran it (celery, django, …), so the Tasks page, search and analysis treat them alike. When the queue can carry
the enqueuing span to the worker (Celery headers), the run links to it at once; when it can't (Django Tasks), the
store links them by `task.id`.
"""

import contextlib
from dataclasses import dataclass

from tracepanel.core.frames import safe_repr
from tracepanel.core.model import Trace

MAX_REPR = 2000
# The caller's trace kind -> task.trigger.
TRIGGERS = {"http": "request", "task": "task", "command": "command", "websocket": "request"}


@dataclass(slots=True)
class TaskRun:
    """A task run in progress: the open trace context and the trace itself."""

    context: contextlib.AbstractContextManager[Trace]
    trace: Trace


def task_repr(name: str, value: object) -> str:
    """Arguments and results for local debugging: masked like locals, cut to a readable size."""
    text = safe_repr(name, value)
    return text if len(text) <= MAX_REPR else f"{text[:MAX_REPR]}…"


def trigger(caller: Trace | None) -> str:
    return TRIGGERS.get(caller.kind, "other") if caller is not None else "other"
