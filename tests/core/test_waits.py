import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from time import sleep as nap
from types import FrameType

from tracepanel.core import waits


def test_the_standard_librarys_waiting_functions_are_known_by_name() -> None:
    assert waits.waits("selectors", "EpollSelector.select")
    assert waits.waits("selectors", "KqueueSelector.select")
    assert waits.waits("asyncio.tasks", "sleep")
    assert waits.waits("threading", "Event.wait")
    assert waits.waits("queue", "Queue.get")

    assert not waits.waits("selectors", "EpollSelector.register")
    assert not waits.waits("app.reports", "sleep")
    assert not waits.waits(None, "Event.wait")


def frame_of(target: Callable[[], object]) -> tuple[FrameType, Callable[[], None]]:
    """The innermost frame of a thread running `target`, once it is inside it; and how to end the thread."""
    started = threading.Event()

    def run() -> None:
        started.set()
        target()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    started.wait()
    time.sleep(0.05)
    assert thread.ident is not None
    frame = sys._current_frames()[thread.ident]  # noqa: SLF001
    return frame, lambda: thread.join(5)


def sleeps() -> None:
    time.sleep(0.2)


def naps() -> None:
    nap(0.2)


def computes() -> None:
    end = time.perf_counter() + 0.2
    while time.perf_counter() < end:
        pass


def test_a_c_call_that_waits_is_read_from_the_innermost_frame() -> None:
    for target, expected in ((sleeps, "time.sleep"), (naps, "time.sleep"), (computes, None)):
        frame, done = frame_of(target)
        called = waits.calling(frame)
        done()

        assert (target.__name__, called) == (target.__name__, expected)


def test_a_method_that_waits_on_a_local_object_is_known_by_its_name() -> None:
    left, right = socket.socketpair()
    reader, writer = os.pipe()

    def receives() -> None:
        left.recv(1)

    def reads() -> None:
        os.read(reader, 1)

    try:
        for target, expected, wake in (
            (receives, "left.recv", lambda: right.send(b"x")),
            (reads, "os.read", lambda: os.write(writer, b"x")),
        ):
            frame, done = frame_of(target)
            called = waits.calling(frame)
            wake()
            done()

            assert called == expected
    finally:
        for closing in (left.close, right.close, lambda: os.close(reader), lambda: os.close(writer)):
            closing()
