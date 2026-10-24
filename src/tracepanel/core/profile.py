"""Where a trace's time goes in code: a sampling profiler (the trace's `profile`, a flame graph in the console).

One daemon thread looks at the stacks of the threads running a profiled trace every `profile_interval_ms`, and counts
each stack it sees. It starts with the first profiled trace and stops once none has run for a while. A thread is
sampled only while it runs exactly one profiled trace: an event loop running several at once can't tell from outside
which one a frame belongs to, so their samples are dropped for that time.

A trace's work can hop to another thread: an ASGI server runs a sync view through sync_to_async, a sync endpoint
through run_in_threadpool, in a worker thread, while the event loop's thread waits in its selector. The hop follows it
(`followed()`): the worker thread is sampled for the trace until the call returns, and the thread it came from is not.

A trace that asked to be profiled (a request's header or cookie) hands the request on with the work it starts: a
task's message, a job's meta, a worker channel's message carries a flag next to its link (`carry()`), and the run
that reads it (`asked_by_parent()`) is profiled too, and so are the tasks it sends in turn. The `profile` setting
profiles every trace anyway: its traces carry nothing. Calls to other apps over HTTP don't carry it: they may not be
the app's own.
"""

import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Generator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from types import CodeType, FrameType
from typing import ParamSpec, TypeVar

from tracepanel.core import conf, forks
from tracepanel.core.frames import is_in_app, library_path, relative

HEADER = "x-tracepanel-profile"
COOKIE = "tracepanel_profile"
# What the work a profiled trace hands off carries under each queue's own key: its run is profiled too.
CARRIED = "1"
# How deep a stack is walked: a recursion beyond it is cut, outermost frames kept.
MAX_DEPTH = 128
# Distinct stacks one trace keeps; the rest are counted as one "(other)" frame, so a profile stays small.
MAX_STACKS = 2000
# Ticks without a profiled trace before the sampler thread ends (it starts again with the next one).
IDLE_TICKS = 400
# This package's own frames say nothing about the app.
_OWN = "tracepanel"

# A function as the stacks name it: its code and the module it runs in.
Key = tuple[CodeType | None, str | None]
OTHER: Key = (None, "(other)")

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(slots=True, eq=False)
class Recording:
    """One trace's samples, as the sampler counts them in its thread."""

    thread_id: int
    interval_us: int
    stacks: Counter[tuple[Key, ...]] = field(default_factory=Counter)
    samples: int = 0
    # Calls running its work in other threads: meanwhile its own thread only waits for them, and is not sampled.
    away: int = 0
    # The trace asked to be profiled (not only the setting): the work it hands off asks too (carry()).
    asked: bool = False


# The recording of the trace running in this context: the work a worker thread does for it is sampled for it too.
current: ContextVar[Recording | None] = ContextVar("tracepanel_profile", default=None)


class Sampler:
    """The thread that samples, and which thread runs which profiled trace."""

    def __init__(self) -> None:
        self._lock = forks.lock()
        self._threads: dict[int, list[Recording]] = {}
        self._thread: threading.Thread | None = None
        # The interpreter's GIL switch interval before the first profiled trace, put back after the last one.
        self._switch: float | None = None

    def start(self, *, asked: bool = False) -> Recording:
        """Begin recording the calling thread's stacks for a trace it starts; `asked`: the trace asked for it."""
        interval_ms = max(conf.get_config().profile_interval_ms, 1)
        recording = Recording(thread_id=threading.get_ident(), interval_us=interval_ms * 1000, asked=asked)
        with self._lock:
            if not self._threads:
                self._quicken(interval_ms / 1000)
            self._threads.setdefault(recording.thread_id, []).append(recording)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, args=(interval_ms / 1000,), name="tracepanel-profiler", daemon=True
                )
                self._thread.start()
        return recording

    def stop(self, recording: Recording) -> dict[str, object] | None:
        """End a trace's recording: its profile, or None when no sample saw it."""
        with self._lock:
            running = self._threads.get(recording.thread_id, [])
            if recording in running:
                running.remove(recording)
            if not running:
                self._threads.pop(recording.thread_id, None)
            if not self._threads:
                self._slow_down()
        return document(recording)

    @contextmanager
    def follow(self, recording: Recording) -> Generator[None, None, None]:
        """Sample the calling thread for a trace whose work hops into it, and not the thread it came from, meanwhile.

        Work that comes back to the trace's own thread (a sync view under WSGI, after async_to_sync) is sampled there.
        """
        thread_id = threading.get_ident()
        if thread_id == recording.thread_id:
            yield
            return
        with self._lock:
            recording.away += 1
            self._threads.setdefault(thread_id, []).append(recording)
        try:
            yield
        finally:
            with self._lock:
                recording.away -= 1
                running = self._threads.get(thread_id, [])
                if recording in running:
                    running.remove(recording)
                if not running:
                    self._threads.pop(thread_id, None)
                if not self._threads:
                    self._slow_down()

    def _quicken(self, interval_s: float) -> None:
        """Hand the GIL over as often as the sampler wakes, while traces are profiled.

        A sleeping thread gets the GIL back only when the running one gives it up: every switch interval (5 ms), and
        in practice 7 to 30 ms while it runs Python code. The sampler would see a busy request a few times, and a
        short one not at all. Only profiled traces pay for the faster switching.
        """
        current = sys.getswitchinterval()
        if current > interval_s:
            self._switch = current
            sys.setswitchinterval(interval_s)

    def _slow_down(self) -> None:
        if self._switch is not None:
            sys.setswitchinterval(self._switch)
            self._switch = None

    def _run(self, interval_s: float) -> None:
        idle = 0
        while True:
            time.sleep(interval_s)
            with self._lock:
                if not self._threads:
                    idle += 1
                    if idle >= IDLE_TICKS:
                        # Decided under the lock that start() takes: a trace starting now finds no thread and starts
                        # one, rather than counting on this one, which is leaving.
                        self._thread = None
                        return
                    continue
                alone = _alone(self._threads)
            idle = 0
            if alone:
                self._sample(alone)

    @staticmethod
    def _sample(recordings: list[tuple[int, Recording]]) -> None:
        frames = sys._current_frames()  # noqa: SLF001
        for thread_id, recording in recordings:
            frame = frames.get(thread_id)
            if frame is None:
                continue
            try:
                keys = stack(frame)
            # A thread can change its frames under the walk: that sample is skipped, the sampler goes on.
            except (AttributeError, ValueError, RuntimeError):
                continue
            count(recording, keys)


def _alone(threads: dict[int, list[Recording]]) -> list[tuple[int, Recording]]:
    """Each thread running one profiled trace's work, and that trace: its stack is that trace's.

    A trace's own thread does not run its work while the work is away in another thread (that thread does).
    """
    alone: list[tuple[int, Recording]] = []
    for thread_id, running in threads.items():
        present = [recording for recording in running if not (recording.away and recording.thread_id == thread_id)]
        if len(present) == 1:
            alone.append((thread_id, present[0]))
    return alone


def followed(func: Callable[P, R]) -> Callable[P, R]:
    """`func`, sampled for the current context's profiled trace in whatever thread runs it; as is outside one.

    For a call handed to a worker thread while the trace's own thread waits for it.
    """
    recording = current.get()
    if recording is None:
        return func

    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        with SAMPLER.follow(recording):
            return func(*args, **kwargs)

    return call


def stack(frame: FrameType | None) -> tuple[Key, ...]:
    """A thread's stack, outermost frame first, without this package's frames."""
    keys: list[Key] = []
    while frame is not None and len(keys) < MAX_DEPTH:
        module = frame.f_globals.get("__name__")
        name = module if isinstance(module, str) else None
        if not (name or "").startswith(_OWN):
            keys.append((frame.f_code, name))
        frame = frame.f_back
    keys.reverse()
    return tuple(keys)


def count(recording: Recording, keys: tuple[Key, ...]) -> None:
    """Count one sample of a stack; past MAX_STACKS distinct ones, a new stack counts as "(other)"."""
    if not keys:
        return
    recording.samples += 1
    if keys in recording.stacks or len(recording.stacks) < MAX_STACKS:
        recording.stacks[keys] += 1
    else:
        recording.stacks[(OTHER,)] += 1


def document(recording: Recording) -> dict[str, object] | None:
    """The trace's `profile`: the functions the stacks name, each once, and each stack as indexes into them."""
    if not recording.samples:
        return None
    index: dict[Key, int] = {}
    frames: list[dict[str, object]] = []
    stacks: list[dict[str, object]] = []
    for keys, samples in recording.stacks.most_common():
        indexes = []
        for key in keys:
            if key not in index:
                index[key] = len(frames)
                frames.append(_frame(key))
            indexes.append(index[key])
        stacks.append({"frames": indexes, "count": samples})
    return {"interval_us": recording.interval_us, "samples": recording.samples, "frames": frames, "stacks": stacks}


def _frame(key: Key) -> dict[str, object]:
    code, module = key
    if code is None:
        return {"function": module or "(other)", "module": None, "file": "", "line": 0, "in_app": False}
    in_app = is_in_app(code.co_filename, module or "")
    return {
        "function": code.co_qualname,
        "module": module,
        "file": relative(code.co_filename) if in_app else library_path(code.co_filename),
        "line": code.co_firstlineno,
        "in_app": in_app,
    }


def requested(header: str | None, cookies: str | None) -> bool:
    """Whether a request asks to be profiled: the X-Tracepanel-Profile header, or the cookie a browser sends."""
    return (header is not None and header.strip() == "1") or by_cookie(cookies)


def by_cookie(cookies: str | None) -> bool:
    """Whether the request carries the panel's cookie: it profiles every request while the panel's toggle is on."""
    if not cookies or COOKIE not in cookies:
        return False
    try:
        jar = SimpleCookie(cookies)
    except CookieError:
        return False
    morsel = jar.get(COOKIE)
    return morsel is not None and morsel.value == "1"


def carry(carrier: MutableMapping[str, object], key: str) -> None:
    """Mark the work the current trace hands off (a message's headers, a job's meta) if it asked to be profiled.

    Under `key`, the queue's own name for it, next to the link the integration puts there. Nothing otherwise: a
    trace the setting profiles, or one not profiled, or any while profiling is off, hands nothing on.
    """
    recording = current.get()
    if recording is not None and recording.asked:
        carrier[key] = CARRIED


def asked_by_parent(flag: object) -> bool:
    """Whether a run asks to be profiled: the work it runs carries the flag a profiled trace put there (carry()).

    Not a run inline in a profiled trace (an eager task, run inside its caller): that trace's profile has its frames.
    """
    return (flag == CARRIED or flag == CARRIED.encode()) and current.get() is None


def wanted(*, asked: bool) -> bool:
    """Whether to profile a trace: the setting profiles every one, a request may ask; nothing when profiling is off."""
    config = conf.get_config()
    return config.profile_enabled and (config.profile or asked)


SAMPLER = Sampler()
