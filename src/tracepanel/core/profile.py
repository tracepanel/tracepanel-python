"""Where a trace's time goes in code: a sampling profiler (the trace's `profile`, a flame graph in the console).

One daemon thread looks at the stacks of the threads running a profiled trace every `profile_interval_ms`, and counts
each stack it sees. It starts with the first profiled trace and stops once none has run for a while. A thread is
sampled only while it runs exactly one profiled trace: an event loop running several at once can't tell from outside
which one a frame belongs to, so their samples are dropped for that time.
"""

import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from types import CodeType, FrameType

from tracepanel.core import conf
from tracepanel.core.frames import is_in_app, relative

HEADER = "x-tracepanel-profile"
COOKIE = "tracepanel_profile"
# The Set-Cookie that ends the panel's one-shot cookie: the request it was sent with is the one profiled.
EXPIRE = f"{COOKIE}=; Max-Age=0; Path=/"
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


@dataclass(slots=True, eq=False)
class Recording:
    """One trace's samples, as the sampler counts them in its thread."""

    thread_id: int
    interval_us: int
    stacks: Counter[tuple[Key, ...]] = field(default_factory=Counter)
    samples: int = 0


class Sampler:
    """The thread that samples, and which thread runs which profiled trace."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[int, list[Recording]] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> Recording:
        """Begin recording the calling thread's stacks for a trace it starts."""
        interval_ms = max(conf.get_config().profile_interval_ms, 1)
        recording = Recording(thread_id=threading.get_ident(), interval_us=interval_ms * 1000)
        with self._lock:
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
        return document(recording)

    def _run(self, interval_s: float) -> None:
        idle = 0
        while idle < IDLE_TICKS:
            time.sleep(interval_s)
            with self._lock:
                # One profiled trace on a thread: its stack is that trace's.
                alone = [running[0] for running in self._threads.values() if len(running) == 1]
                if not self._threads:
                    idle += 1
                    continue
            idle = 0
            if alone:
                self._sample(alone)
        with self._lock:
            self._thread = None

    @staticmethod
    def _sample(recordings: list[Recording]) -> None:
        frames = sys._current_frames()  # noqa: SLF001
        for recording in recordings:
            frame = frames.get(recording.thread_id)
            if frame is None:
                continue
            try:
                keys = stack(frame)
            # A thread can change its frames under the walk: that sample is skipped, the sampler goes on.
            except (AttributeError, ValueError, RuntimeError):
                continue
            count(recording, keys)


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
        "file": relative(code.co_filename) if in_app else code.co_filename,
        "line": code.co_firstlineno,
        "in_app": in_app,
    }


def requested(header: str | None, cookies: str | None) -> bool:
    """Whether a request asks to be profiled: the X-Tracepanel-Profile header, or the cookie a browser sends."""
    return (header is not None and header.strip() == "1") or by_cookie(cookies)


def by_cookie(cookies: str | None) -> bool:
    """Whether the request carries the panel's one-shot cookie: the response then deletes it (EXPIRE)."""
    if not cookies or COOKIE not in cookies:
        return False
    try:
        jar = SimpleCookie(cookies)
    except CookieError:
        return False
    morsel = jar.get(COOKIE)
    return morsel is not None and morsel.value == "1"


def wanted(*, asked: bool) -> bool:
    """Whether to profile a trace: the setting profiles every one, a request may ask; nothing when profiling is off."""
    config = conf.get_config()
    return config.profile_enabled and (config.profile or asked)


SAMPLER = Sampler()
