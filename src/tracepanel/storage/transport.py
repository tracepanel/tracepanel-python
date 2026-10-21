"""Bounded buffer and background batch writing.

Traces go to the project's tracepanel-agent (see tracepanel.storage.agent), which keeps them and finds their issues.
So do task events (send_event): a task's steps while it is in flight, before its trace exists.
"""

import atexit
import contextlib
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from tracepanel.core import conf, errors, forks, runtime
from tracepanel.core.model import Trace

QUEUE_SIZE = 1000
BATCH_SIZE = 50
# After the first trace, how long the writer waits for more before writing: fewer commits under steady traffic. A task
# event doesn't wait: a task shorter than the wait would be started and finished at once, never seen running.
BATCH_WAIT_S = 0.05


# A task event: a small JSON object (see tracepanel.integrations._tasks.task_event).
Event = dict[str, Any]


class Writer(Protocol):
    def submit(self, trace: Trace) -> None: ...
    def flush(self, timeout: float = 5.0) -> None: ...


class BatchWriter:
    """A bounded queue and a thread that writes it in batches; subclasses say where a batch goes (`write`).

    Never blocks the request: a full queue drops the trace and counts it.
    """

    def __init__(self) -> None:
        self.dropped = 0
        self._start()
        atexit.register(self.flush)
        # A forked child (a work horse, a pool's process) starts with a queue and a thread of its own.
        forks.after_fork(self._start)

    def _start(self) -> None:
        self._queue: queue.Queue[Trace | Event] = queue.Queue(QUEUE_SIZE)
        self._thread: threading.Thread | None = None

    def submit(self, trace: Trace) -> None:
        self._put(trace)

    def submit_event(self, event: Event) -> None:
        """A task event, sent in order with the traces: a task's trace goes before the event that it finished."""
        self._put(event)

    def _put(self, item: Trace | Event) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="tracepanel-writer", daemon=True)
            self._thread.start()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            # A task event goes at once, with whatever is queued already.
            deadline = time.monotonic() + (BATCH_WAIT_S if isinstance(batch[0], Trace) else 0)
            while len(batch) < BATCH_SIZE:
                try:
                    item = self._queue.get(timeout=max(deadline - time.monotonic(), 0))
                except queue.Empty:
                    break
                batch.append(item)
                if not isinstance(item, Trace):
                    deadline = 0.0
            traces = [item for item in batch if isinstance(item, Trace)]
            events = [item for item in batch if not isinstance(item, Trace)]
            try:
                if traces:
                    self.write(traces)
                if events:
                    self.write_events(events)
            except Exception:  # noqa: BLE001 (the writer thread must survive storage errors)
                self.dropped += len(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def write(self, batch: list[Trace]) -> None:
        raise NotImplementedError

    def write_events(self, events: list[Event]) -> None:
        """Where task events go: nowhere, unless a subclass sends them."""
        del events

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until queued traces are written (used at exit and in tests)."""
        done = threading.Event()

        def wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=wait, daemon=True).start()
        done.wait(timeout)


_writer: Writer | None = None


def get_writer() -> Writer:
    global _writer  # noqa: PLW0603 (lazy process-wide singleton)
    if _writer is None:
        from tracepanel.storage import agent  # noqa: PLC0415 (agent imports this module)

        _writer = agent.AgentWriter(conf.get_config())
    return _writer


def set_writer(writer: Writer | None) -> None:
    global _writer  # noqa: PLW0603
    _writer = writer


def send_event(event: Event) -> None:
    """A task event to the agent, if the writer sends them (a test's collector doesn't); never blocks."""
    submit = getattr(get_writer(), "submit_event", None)
    if submit is not None:
        submit(event)


def role(trace: Trace) -> str:
    """Which part of the project produced the trace: web, worker or command."""
    if trace.kind == "task":
        return "worker"
    return {"http": "web"}.get(trace.kind, trace.kind)


# Called with every finished trace after it is handed to the writer (the console's terminal line).
_listeners: list[Callable[[Trace], None]] = []


def on_complete(listener: Callable[[Trace], None]) -> None:
    if listener not in _listeners:
        _listeners.append(listener)


def complete(trace: Trace) -> Trace:
    """Hand a finished trace to the writer: the last step for requests, tasks, commands, `trace()`."""
    # Only requests use it (a 5xx behind a logged exception); drop it for every trace.
    errors.pop_logged_exception(trace.id)
    config = conf.get_config()
    trace.attrs.setdefault("service.name", config.service)
    trace.attrs.setdefault("service.role", role(trace))
    for key, value in runtime.attrs().items():
        trace.attrs.setdefault(key, value)
    get_writer().submit(trace)
    for listener in _listeners:
        # A listener (the terminal line) must never break the traced code.
        with contextlib.suppress(Exception):
            listener(trace)
    return trace
