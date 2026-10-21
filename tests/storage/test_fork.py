"""A forked worker (RQ's work horse, Celery's prefork pool, gunicorn --preload) sends on a connection of its own.

The fork copies only the thread that forks: the writer's thread stays behind, and whatever it held stays held.
"""

import dataclasses
import json
import os
import signal
import socket
import struct
import threading
import time
from collections.abc import Generator

import pytest
from dirty_equals import IsPartialDict

from tracepanel._compat import override
from tracepanel.core import conf, forks
from tracepanel.core.context import start_trace
from tracepanel.core.model import Trace
from tracepanel.storage import agent, transport

pytestmark = [
    pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork on this platform"),
    # Forking with threads running is what these tests do on purpose.
    pytest.mark.filterwarnings("ignore:.*multi-threaded.*fork:DeprecationWarning"),
]


class Agent:
    """An agent's socket, as this test reads it: each connection's hello, then its frames, decoded."""

    def __init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.connections: list[list[object]] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def address(self) -> str:
        host, port = self.listener.getsockname()
        return f"tcp:{host}:{port}"

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._read, args=(connection,), daemon=True).start()

    def _read(self, connection: socket.socket) -> None:
        received: list[object] = []
        with self._lock:
            self.connections.append(received)
        with connection, connection.makefile("rb") as stream:
            received.append(json.loads(stream.readline()))
            connection.sendall(b'{"ok": true}\n')
            while len(header := stream.read(4)) == 4:
                data = stream.read(struct.unpack(">I", header)[0])
                try:
                    received.append(json.loads(data))
                except ValueError:
                    # Two writers' bytes in one stream: no longer a frame.
                    received.append("<garbled>")

    def wait_for(self, frames: int, timeout: float = 10.0) -> None:
        """Until the connections together have this many frames past their hellos."""
        deadline = time.monotonic() + timeout
        while sum(len(received) - 1 for received in self.connections) < frames and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.fixture
def listening() -> Generator[Agent, None, None]:
    agent_socket = Agent()
    yield agent_socket
    agent_socket.listener.close()


def traced(name: str) -> Trace:
    with start_trace("task", name) as trace:
        pass
    return trace


def wait_for_exit(pid: int, timeout: float) -> int | None:
    """The child's exit code; None (and the child killed) if it hasn't exited by then."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return None


def test_a_child_forked_while_the_writer_was_sending_sends_on_its_own_connection(listening: Agent) -> None:
    writer = agent.AgentWriter(dataclasses.replace(conf.get_config(), agent=listening.address))
    first, child_trace, later = traced("parent first"), traced("child"), traced("parent later")
    event = {"event": "started", "system": "rq", "id": "job-1", "time": 1}
    writer.submit(first)
    writer.flush()
    # The parent's writer thread is sending (a heartbeat, a batch) as the worker forks its work horse.
    sending, release = threading.Event(), threading.Event()

    def send() -> None:
        with writer._lock:  # noqa: SLF001
            sending.set()
            release.wait(10)

    holder = threading.Thread(target=send)
    holder.start()
    sending.wait(5)
    pid = os.fork()
    if pid == 0:
        # The work horse: its trace and its task event, then gone, as RQ's horse ends (os._exit).
        code = 1
        try:
            # The counts are the parent's so far: the first trace went.
            before = (writer.sent, writer.dropped)
            writer.submit(child_trace)
            writer.submit_event(event)
            writer.flush(timeout=5)
            code = 0 if (before, (writer.sent, writer.dropped)) == ((1, 0), (2, 0)) else 2
        finally:
            os._exit(code)
    exit_code = wait_for_exit(pid, timeout=15)
    release.set()
    holder.join()
    writer.submit(later)
    writer.flush()
    listening.wait_for(4)

    assert exit_code == 0

    assert listening.connections == [
        [
            IsPartialDict(protocol=agent.PROTOCOL),
            [IsPartialDict(id=first.id, name="parent first")],
            [IsPartialDict(id=later.id, name="parent later")],
        ],
        [
            IsPartialDict(protocol=agent.PROTOCOL),
            [IsPartialDict(id=child_trace.id, name="child")],
            {"tasks": [event]},
        ],
    ]


def test_a_lock_held_as_the_process_forks_is_free_in_the_child() -> None:
    lock = forks.lock()
    lock.acquire()
    pid = os.fork()
    if pid == 0:
        os._exit(0 if lock.acquire(timeout=2) else 1)
    exit_code = wait_for_exit(pid, timeout=10)
    lock.release()

    assert exit_code == 0


class Recorder(transport.BatchWriter):
    """What the writer thread writes, with the time each write happened."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []
        super().__init__()

    @override
    def write(self, batch: list[Trace]) -> None:
        self.writes.append(("traces", len(batch)))

    @override
    def write_events(self, events: list[transport.Event]) -> None:
        self.writes.append(("events", len(events)))


def test_a_task_event_is_written_at_once_with_what_is_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wait longer than the flush: anything that waits for it isn't written in time.
    monkeypatch.setattr(transport, "BATCH_WAIT_S", 5.0)
    writer = Recorder()

    writer.submit_event({"event": "started", "system": "rq", "id": "job-1", "time": 1})
    writer.flush(timeout=1)
    started = list(writer.writes)
    writer.submit(traced("short job"))
    writer.submit_event({"event": "finished", "system": "rq", "id": "job-1", "time": 2})
    writer.flush(timeout=1)

    assert started == [("events", 1)]

    # The trace, then its finished event: a trace waiting for more is written as an event comes.
    assert writer.writes == [("events", 1), ("traces", 1), ("events", 1)]
