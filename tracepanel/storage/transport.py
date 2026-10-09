"""Bounded buffer and background batch writing.

Traces go to tracepanel-agent when one runs for the project (see tracepanel.storage.agent), else to the local store.
"""

import atexit
import contextlib
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Protocol

from tracepanel._compat import override
from tracepanel.core import conf, errors, runtime
from tracepanel.core.analysis import analyze
from tracepanel.core.model import Trace
from tracepanel.storage.store import Store, get_store

QUEUE_SIZE = 1000
BATCH_SIZE = 50
# After the first trace, how long the writer waits for more before writing: fewer commits under steady traffic.
BATCH_WAIT_S = 0.05


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
        os.register_at_fork(after_in_child=self._start)

    def _start(self) -> None:
        self._queue: queue.Queue[Trace] = queue.Queue(QUEUE_SIZE)
        self._thread: threading.Thread | None = None

    def submit(self, trace: Trace) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="tracepanel-writer", daemon=True)
            self._thread.start()
        try:
            self._queue.put_nowait(trace)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            deadline = time.monotonic() + BATCH_WAIT_S
            while len(batch) < BATCH_SIZE:
                try:
                    batch.append(self._queue.get(timeout=max(deadline - time.monotonic(), 0)))
                except queue.Empty:
                    break
            try:
                self.write(batch)
            except Exception:  # noqa: BLE001 (the writer thread must survive storage errors)
                self.dropped += len(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def write(self, batch: list[Trace]) -> None:
        raise NotImplementedError

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until queued traces are written (used at exit and in tests)."""
        done = threading.Event()

        def wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=wait, daemon=True).start()
        done.wait(timeout)


class StoreWriter(BatchWriter):
    """Writes the project's local store itself."""

    def __init__(self, store: Store) -> None:
        self.store = store
        super().__init__()

    @override
    def write(self, batch: list[Trace]) -> None:
        self.store.write(batch)


_writer: Writer | None = None


def get_writer() -> Writer:
    global _writer  # noqa: PLW0603 (lazy process-wide singleton)
    if _writer is None:
        from tracepanel.storage import agent  # noqa: PLC0415 (agent imports this module)

        config = conf.get_config()
        # With an agent (`tracepanel agent`) the traces go to it; without one, as before, to the local store.
        _writer = StoreWriter(get_store()) if config.agent == "off" else agent.AgentWriter(config)
    return _writer


def set_writer(writer: Writer | None) -> None:
    global _writer  # noqa: PLW0603
    _writer = writer


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
    """Analyse a finished trace and hand it to the writer: the last step for requests, tasks, commands, `trace()`."""
    # Only requests use it (a 5xx behind a logged exception); drop it for every trace.
    errors.pop_logged_exception(trace.id)
    config = conf.get_config()
    trace.attrs.setdefault("service.name", config.service)
    trace.attrs.setdefault("service.role", role(trace))
    for key, value in runtime.attrs().items():
        trace.attrs.setdefault(key, value)
    trace.issues = analyze(
        trace,
        n_plus_one_threshold=config.n_plus_one_threshold,
        slow_sql_ms=config.slow_sql_ms,
        long_transaction_ms=config.long_transaction_ms,
    )
    get_writer().submit(trace)
    for listener in _listeners:
        # A listener (the terminal line) must never break the traced code.
        with contextlib.suppress(Exception):
            listener(trace)
    return trace
