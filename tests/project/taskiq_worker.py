"""A Taskiq broker for a real `taskiq worker`, run as its own process: the CLI's worker processes, unlike a test's loop.

Its broker is a folder ($TASKIQ_DIR), each message a file, so the test and the worker need nothing running. In the
worker ($TASKIQ_OUT set), what tracepanel writes goes to a file, a line each, from every process alike.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from taskiq import AsyncBroker
from taskiq.message import BrokerMessage

from tracepanel.core.model import Trace
from tracepanel.integrations import taskiq
from tracepanel.storage.transport import set_writer


class FolderBroker(AsyncBroker):
    """Messages as files in a folder: kicked by one process, taken by the first worker process to rename one."""

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.folder = folder
        self.queue_name = "folder"

    async def kick(self, message: BrokerMessage) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{message.task_id}"
        written = self.folder / f"{name}.tmp"
        written.write_bytes(message.message)
        written.rename(self.folder / f"{name}.msg")

    async def listen(self) -> AsyncGenerator[bytes, None]:
        while True:
            for path in sorted(self.folder.glob("*.msg")):
                taken = path.with_suffix(f".{os.getpid()}")
                try:
                    path.rename(taken)
                except FileNotFoundError:
                    continue
                data = taken.read_bytes()
                taken.unlink()
                yield data
            await asyncio.sleep(0.02)


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
                "worker": trace.attrs.get("task.worker"),
                "state": trace.attrs.get("task.state"),
                "links": [link.trace_id for link in trace.links],
                "logs": [log.message for log in trace.logs],
            }
        )

    def submit_event(self, event: dict[str, Any]) -> None:
        self._write(event)

    def flush(self, timeout: float = 5.0) -> None:
        pass


if out := os.environ.get("TASKIQ_OUT"):
    # What `tracepanel run taskiq worker …` does as Taskiq is imported, before the broker exists.
    set_writer(Lines(out))
    taskiq.install()

broker = FolderBroker(Path(os.environ.get("TASKIQ_DIR", ".")))


@broker.task
async def count_users(limit: int) -> int:
    return limit
