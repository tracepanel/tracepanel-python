"""A Celery app for a real prefork worker, run as its own process: a pool process to kill, unlike the tests' threads.

Its broker is a folder ($PREFORK_DIR), so the test and the worker need nothing running. In the worker
($PREFORK_OUT set), what tracepanel writes goes to a file, a line each, from the main process and its pool's alike.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from celery import Celery

from tracepanel.core.model import Trace
from tracepanel.integrations import celery
from tracepanel.storage.transport import set_writer


def transport(folder: Path) -> dict[str, str]:
    """The folder broker's options: its messages and its exchanges both in the folder."""
    return {"data_folder_in": str(folder), "data_folder_out": str(folder), "control_folder": str(folder / "control")}


# Not the current app of a process that imports it: the tests' is tests.project.jobs's.
app = Celery("prefork", broker="filesystem://", backend="cache+memory://", set_as_current=False)
app.conf.update(
    broker_transport_options=transport(Path(os.environ.get("PREFORK_DIR", "."))), worker_hijack_root_logger=False
)


class Lines:
    """A writer that appends each trace and task event to a file, with the process that wrote it."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _write(self, line: dict[str, Any]) -> None:
        with Path(self.path).open("a") as out:
            out.write(json.dumps({"pid": os.getpid(), **line}) + "\n")

    def submit(self, trace: Trace) -> None:
        self._write(
            {
                "trace": trace.attrs.get("task.id"),
                "status": trace.status,
                "state": trace.attrs.get("task.state"),
                "timeout": trace.attrs.get("task.timeout"),
                "errors": [error.type for error in trace.errors],
            }
        )

    def submit_event(self, event: dict[str, Any]) -> None:
        self._write(event)

    def flush(self, timeout: float = 5.0) -> None:
        pass


if out := os.environ.get("PREFORK_OUT"):
    set_writer(Lines(out))
    celery.install()


@app.task(time_limit=1)
def overstays() -> None:
    time.sleep(10)


@app.task
def sleeps() -> None:
    time.sleep(10)
