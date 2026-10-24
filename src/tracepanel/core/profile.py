"""Where a trace's time goes in code: a sampling profiler (the trace's `profile`, a flame graph in the console).

One daemon thread looks at the stacks of the threads running a profiled trace every `profile_interval_ms`, and counts
each stack it sees, with the 10 ms stretch of the trace it was seen in (`bucket_us`, a stack's `at`). It starts with
the first profiled trace and stops once none has run for a while. A frame that waits rather than computes (the event
loop's select, a sleep, a socket read, a lock: core.waits) says so (`waiting`).

Async code: a trace begun in an asyncio Task is that Task's. While the loop runs the Task, the thread's stack is the
trace's, from the Task's coroutine on. While the Task waits, whatever the thread does (sits in select, runs another
request), the trace's stack is the Task's coroutines down to the one that waits (`asyncio.sleep`), the innermost of
the project's own with the line of its `await` (`at_line`). So traces sharing an event loop each get their samples.
A tick whose stack can't be told apart between traces (two sync traces on one thread, a Task running two) is counted
as `dropped` for each.

A trace's work can hop to another thread: an ASGI server runs a sync view through sync_to_async, a sync endpoint
through run_in_threadpool, in a worker thread, while the event loop's thread waits in its selector. The hop follows it
(`followed()`): the worker thread is sampled for the trace until the call returns, and the thread it came from is not.

A trace that asked to be profiled (a request's header or cookie) hands the request on with the work it starts: a
task's message, a job's meta, a worker channel's message carries a flag next to its link (`carry()`), and the run
that reads it (`asked_by_parent()`) is profiled too, and so are the tasks it sends in turn. The `profile` setting
profiles every trace anyway: its traces carry nothing. Calls to other apps over HTTP don't carry it: they may not be
the app's own.

A profiled trace's memory (core.memory) and garbage collections are counted while it runs: each collection's pause,
from gc.callbacks, goes to every profiled trace running then.
"""

import asyncio
import gc
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

from tracepanel.core import conf, forks, memory, waits
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
# The stretch of a trace a sample's time is counted in, from the trace's start.
BUCKET_US = 10_000
# This package's own frames say nothing about the app.
_OWN = "tracepanel"
# GC pauses a trace lists, each with when it began; past it they're only counted.
MAX_PAUSES = 1000

# A function as the stacks name it: its code (None for a C call that waits, named by the module), the module it runs
# in, the line it awaits at (the project's coroutines a Task waits in), and whether it waits though core.waits doesn't
# list it (what a waiting Task awaits last, a C call that waits).
Key = tuple[CodeType | None, str | None, int | None, bool]
OTHER: Key = (None, "(other)", None, False)

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(slots=True, eq=False)
class Recording:
    """One trace's samples, as the sampler counts them in its thread."""

    thread_id: int
    interval_us: int
    # Each stack seen, with how many samples saw it in each BUCKET_US stretch of the trace.
    stacks: dict[tuple[Key, ...], Counter[int]] = field(default_factory=dict)
    samples: int = 0
    # Calls running its work in other threads: meanwhile its own thread only waits for them, and is not sampled.
    away: int = 0
    # The trace asked to be profiled (not only the setting): the work it hands off asks too (carry()).
    asked: bool = False
    # When it began (perf_counter_ns): a sample's stretch counts from it.
    started: int = 0
    # The event loop and Task a trace begun in async code runs in: what the Task awaits is the trace's while it waits.
    loop: asyncio.AbstractEventLoop | None = None
    task: "asyncio.Task[object] | None" = None
    # Ticks whose stack couldn't be told apart from another trace's on the same thread.
    dropped: int = 0
    # What it allocated (core.memory); None when tracemalloc couldn't trace.
    heap: memory.Memory | None = None
    # Another trace ran in the process meanwhile: its allocations and pauses are in this one's too.
    shared: bool = False
    # Garbage collections during the trace: how many, the time they stopped it, and each [began, lasted, generation]
    # in microseconds from the trace's start.
    collections: int = 0
    pause_ns: int = 0
    pauses: list[list[int]] = field(default_factory=list)


# The recording of the trace running in this context: the work a worker thread does for it is sampled for it too.
current: ContextVar[Recording | None] = ContextVar("tracepanel_profile", default=None)

# The traces running in the process, profiled or not (core.context): a profiled one beside others is `shared`.
LIVE: set[object] = set()
forks.after_fork(LIVE.clear)


class Sampler:
    """The thread that samples, and which thread runs which profiled trace."""

    def __init__(self) -> None:
        self._lock = forks.lock()
        self._threads: dict[int, list[Recording]] = {}
        self._thread: threading.Thread | None = None
        # The interpreter's GIL switch interval before the first profiled trace, put back after the last one.
        self._switch: float | None = None
        self._tracer = memory.TRACER
        # The profiled traces running, each once: read without the lock where taking it could deadlock (a collection
        # can begin in any allocation, the lock's holder's too), replaced whole under it.
        self.running: tuple[Recording, ...] = ()
        self._collecting = 0

    def start(self, *, asked: bool = False) -> Recording:
        """Begin recording the calling thread's stacks for a trace it starts; `asked`: the trace asked for it."""
        interval_ms = max(conf.get_config().profile_interval_ms, 1)
        loop, task = _running_task()
        traced = self._tracer.begin()
        recording = Recording(
            thread_id=threading.get_ident(),
            interval_us=interval_ms * 1000,
            asked=asked,
            started=time.perf_counter_ns(),
            loop=loop,
            task=task,
            heap=traced,
        )
        with self._lock:
            if not self._threads:
                self._quicken(interval_ms / 1000)
                gc.callbacks.append(self._collected)
            self._threads.setdefault(recording.thread_id, []).append(recording)
            self.running = (*self.running, recording)
            # Another trace runs already, profiled or not (LIVE has this one's too); the ones beginning later say so
            # (overlap()).
            if len(LIVE) > 1 or len(self.running) > 1:
                self.overlap()
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
            self.running = tuple(other for other in self.running if other is not recording)
            if not self._threads:
                self._slow_down()
        heap, recording.heap = recording.heap, None
        measured = None
        if heap is not None:
            # No profile, nothing to measure for (document()): the trace's memory only stops being watched.
            if recording.samples or recording.dropped:
                measured = memory.measure(heap, self._tracer)
            else:
                self._tracer.end(heap)
        return document(recording, measured)

    def overlap(self) -> None:
        """A trace begins: the profiled ones running share the process with it."""
        for recording in self.running:
            recording.shared = True

    def _collected(self, phase: str, info: dict[str, int]) -> None:
        """A garbage collection's start or stop (gc.callbacks): its pause, for each profiled trace running."""
        if phase == "start":
            self._collecting = time.perf_counter_ns()
            return
        began, self._collecting = self._collecting, 0
        if not began:
            return
        lasted = time.perf_counter_ns() - began
        for recording in self.running:
            recording.collections += 1
            recording.pause_ns += lasted
            if len(recording.pauses) < MAX_PAUSES:
                at = max(began - recording.started, 0) // 1000
                recording.pauses.append([at, lasted // 1000, info.get("generation", 0)])

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
        if self._collected in gc.callbacks:
            gc.callbacks.remove(self._collected)
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
                        self._tracer.idle()
                        return
                    continue
                present = _present(self._threads)
            idle = 0
            if present:
                _sample(present)
            self._tracer.watch()


def _running_task() -> "tuple[asyncio.AbstractEventLoop | None, asyncio.Task[object] | None]":
    """The event loop and Task the calling code runs in; (None, None) outside a Task."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None, None
    task = asyncio.current_task(loop)
    return (loop, task) if task is not None else (None, None)


def _present(threads: dict[int, list[Recording]]) -> list[tuple[int, list[Recording]]]:
    """Each thread running profiled traces' work, and those traces.

    A trace's own thread does not run its work while the work is away in another thread (that thread does).
    """
    present: list[tuple[int, list[Recording]]] = []
    for thread_id, running in threads.items():
        here = [recording for recording in running if not (recording.away and recording.thread_id == thread_id)]
        if here:
            present.append((thread_id, here))
    return present


def _sample(present: list[tuple[int, list[Recording]]]) -> None:
    frames = sys._current_frames()  # noqa: SLF001
    now = time.perf_counter_ns()
    try:
        for thread_id, recordings in present:
            frame = frames.get(thread_id)
            if frame is None:
                continue
            try:
                _attribute(thread_id, frame, recordings, now)
            # A thread can change its frames under the walk: that sample is skipped, the sampler goes on.
            except (AttributeError, ValueError, RuntimeError, TypeError):
                continue
    finally:
        # The frames hold this one, which holds them: a cycle that would keep every sampled frame, and its locals
        # (a request's rows), until the next garbage collection.
        frames.clear()


def _attribute(thread_id: int, frame: FrameType, recordings: list[Recording], now: int) -> None:
    """Count one tick of a thread: its stack for the trace running there, what it awaits for each async one waiting.

    Several traces whose stack can't be told apart each count the tick as dropped.
    """
    in_tasks = [
        recording for recording in recordings if recording.task is not None and recording.thread_id == thread_id
    ]
    if not in_tasks:
        if len(recordings) == 1:
            count(recordings[0], thread_stack(frame), now)
        else:
            for recording in recordings:
                recording.dropped += 1
        return
    running = asyncio.current_task(in_tasks[0].loop)
    if running is None:
        # The loop sits in select or runs a callback: the thread's stack is a sync trace's, if any.
        owners = [recording for recording in recordings if recording not in in_tasks]
    else:
        owners = _owners(running, in_tasks)
    if len(owners) == 1:
        count(owners[0], thread_stack(frame, running), now)
    else:
        for recording in owners:
            recording.dropped += 1
    for recording in in_tasks:
        if recording in owners:
            continue
        if recording.task is running:
            # Its Task runs what another trace started inside it, not profiled on its own.
            recording.dropped += 1
            continue
        count(recording, awaiting(recording.task), now)


def _owners(running: "asyncio.Task[object]", recordings: list[Recording]) -> list[Recording]:
    """The traces whose work the running Task does: the one current in its context, else the ones begun in it.

    A Task the trace started (gather, create_task) has its context, so its work is the trace's too; Python 3.11's Task
    doesn't show its context.
    """
    get_context = getattr(running, "get_context", None)
    if get_context is not None:
        found = get_context().get(current)
        if any(recording is found for recording in recordings):
            return [found]
    return [recording for recording in recordings if recording.task is running]


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


def stack(frame: FrameType | None, root: CodeType | None = None) -> tuple[Key, ...]:
    """A thread's stack, outermost frame first, without this package's frames; from `root`'s frame on, if given."""
    keys: list[Key] = []
    while frame is not None and len(keys) < MAX_DEPTH:
        name = _module(frame)
        code = frame.f_code
        if not (name or "").startswith(_OWN):
            keys.append((code, name, None, False))
        if code is root:
            break
        frame = frame.f_back
    keys.reverse()
    return tuple(keys)


def thread_stack(frame: FrameType, task: "asyncio.Task[object] | None" = None) -> tuple[Key, ...]:
    """What a thread runs: its stack, from the running Task's coroutine on, and the C call that waits, if any."""
    keys = stack(frame, _code(task.get_coro()) if task is not None else None)
    waited = waits.calling(frame) if keys else None
    return (*keys, (None, waited, None, True)) if waited is not None else keys


def awaiting(task: "asyncio.Task[object] | None") -> tuple[Key, ...]:
    """What a waiting Task awaits: its coroutines, outermost first, down to the one that waits, which says so.

    The innermost of the project's own coroutines says the line of its `await` (`at_line`): where the app waits. The
    others read as the same frames as in the stacks of the Task running.
    """
    keys: list[Key] = []
    awaits_at: tuple[int, int] | None = None
    awaited: object = task.get_coro() if task is not None else None
    while awaited is not None and len(keys) < MAX_DEPTH:
        frame = (
            getattr(awaited, "cr_frame", None)
            or getattr(awaited, "gi_frame", None)
            or getattr(awaited, "ag_frame", None)
        )
        if not isinstance(frame, FrameType):
            break
        name = _module(frame)
        code = frame.f_code
        if not (name or "").startswith(_OWN):
            if is_in_app(code.co_filename, name or ""):
                awaits_at = (len(keys), frame.f_lineno)
            keys.append((code, name, None, False))
        awaited = (
            getattr(awaited, "cr_await", None)
            or getattr(awaited, "gi_yieldfrom", None)
            or getattr(awaited, "ag_await", None)
        )
    if awaits_at is not None:
        position, line = awaits_at
        code, name, _, _ = keys[position]
        keys[position] = (code, name, line, False)
    if keys:
        code, name, line, _ = keys[-1]
        if code is not None and not waits.waits(name, code.co_qualname):
            keys[-1] = (code, name, line, True)
    return tuple(keys)


def _module(frame: FrameType) -> str | None:
    module = frame.f_globals.get("__name__")
    return module if isinstance(module, str) else None


def _code(awaitable: object) -> CodeType | None:
    code = getattr(awaitable, "cr_code", None) or getattr(awaitable, "gi_code", None)
    return code if isinstance(code, CodeType) else None


def count(recording: Recording, keys: tuple[Key, ...], now: int | None = None) -> None:
    """Count one sample of a stack, seen `now` (perf_counter_ns; the trace's start if not given).

    Past MAX_STACKS distinct ones, a new stack counts as "(other)".
    """
    if not keys:
        return
    recording.samples += 1
    at = max(now - recording.started, 0) // (BUCKET_US * 1000) if now is not None else 0
    seen = recording.stacks.get(keys)
    if seen is None:
        if len(recording.stacks) >= MAX_STACKS:
            keys = (OTHER,)
        seen = recording.stacks.setdefault(keys, Counter())
    seen[at] += 1


def document(recording: Recording, measured: memory.Measured | None = None) -> dict[str, object] | None:
    """The trace's `profile`: the functions the stacks name, each once, and each stack as indexes into them.

    With what it allocated (`measured`, core.memory), its sites' frames in the same table, and its GC pauses. None
    when nothing was seen; a trace whose ticks were all dropped has a profile that says so.
    """
    if not recording.samples and not recording.dropped:
        return None
    table = _Table()
    stacks: list[dict[str, object]] = []
    seen = sorted(recording.stacks.items(), key=lambda item: -sum(item[1].values()))
    for keys, at in seen:
        stacks.append(
            {
                "frames": [table.of(key) for key in keys],
                "count": sum(at.values()),
                "at": [[bucket, at[bucket]] for bucket in sorted(at)],
            }
        )
    profile: dict[str, object] = {
        "interval_us": recording.interval_us,
        "samples": recording.samples,
        "dropped": recording.dropped,
        "bucket_us": BUCKET_US,
        "frames": table.frames,
        "stacks": stacks,
    }
    if measured is not None:
        totals, sites = measured
        named = [
            {"frames": [table.of(key) for key in keys], "bytes": size, "count": blocks} for keys, size, blocks in sites
        ]
        profile["memory"] = {**totals, "sites": named, "shared": recording.shared}
    profile["gc"] = {
        "collections": recording.collections,
        "pause_us": recording.pause_ns // 1000,
        "pauses": recording.pauses,
    }
    return profile


class _Table:
    """A profile's `frames`: each function its stacks and its memory's sites name, once."""

    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []
        self._index: dict[memory.SiteKey, int] = {}

    def of(self, key: memory.SiteKey) -> int:
        """The frame's index, added if new."""
        found = self._index.get(key)
        if found is None:
            found = self._index[key] = len(self.frames)
            match key:
                # A site's frame named by its file and line (memory.Place), not by a function.
                case (str() as filename, int() as line):
                    self.frames.append(_place(filename, line))
                case (code, module, at_line, waiting):
                    self.frames.append(_frame((code, module, at_line, waiting)))
        return found


def _frame(key: Key) -> dict[str, object]:
    code, module, at_line, waiting = key
    if code is None:
        named: dict[str, object] = {
            "function": module or "(other)",
            "module": None,
            "file": "",
            "line": 0,
            "in_app": False,
        }
        if waiting:
            named["waiting"] = True
        return named
    in_app = is_in_app(code.co_filename, module or "")
    frame: dict[str, object] = {
        "function": code.co_qualname,
        "module": module,
        "file": relative(code.co_filename) if in_app else library_path(code.co_filename),
        "line": code.co_firstlineno,
        "in_app": in_app,
    }
    if at_line is not None:
        frame["at_line"] = at_line
    if waiting or waits.waits(module, code.co_qualname):
        frame["waiting"] = True
    return frame


def _place(filename: str, line: int) -> dict[str, object]:
    """A site's frame where no function has the line: a module's top level."""
    in_app = is_in_app(filename, "")
    return {
        "function": "<module>",
        "module": None,
        "file": relative(filename) if in_app else library_path(filename),
        "line": line,
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
