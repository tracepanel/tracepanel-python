"""What every task queue integration shares: the `task.*` model, independent of the queue.

A task run is a `task` trace (root span `task.run`), an enqueue a `task.enqueue` span; `task.system` says which
queue ran it (celery, django, …), so the Tasks page, search and analysis treat them alike. When the queue can carry
the enqueuing span to the worker (Celery headers), the run links to it at once; when it can't (Django Tasks), the
store links them by `task.id`.

While a task is in flight, before its trace exists, task events tell the agent where it is: `enqueued`, `started`,
`retried`, `finished`, and a worker's `heartbeat`. The console lists the tasks queued, running and waiting to be
retried from them (the Tasks page's Live). Any queue sends the same events; `system` says which one.
"""

import contextlib
from dataclasses import dataclass

from tracepanel.core.context import now_us
from tracepanel.core.frames import safe_repr
from tracepanel.core.model import Trace
from tracepanel.storage.transport import send_event

MAX_REPR = 2000
# Arguments in a task event are for a glance at a list: shorter than in the trace.
MAX_EVENT_REPR = 200
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


def task_event(event: str, system: str, task_id: str, **fields: object) -> None:
    """A task event to the agent: what this step knows, None fields left out."""
    known = {key: value for key, value in fields.items() if value is not None}
    send_event({"event": event, "system": system, "id": task_id, "time": now_us(), **known})


def event_repr(name: str, value: object) -> str:
    """Arguments for a task event: masked like locals, cut to a glance."""
    text = safe_repr(name, value)
    return text if len(text) <= MAX_EVENT_REPR else f"{text[:MAX_EVENT_REPR]}…"
