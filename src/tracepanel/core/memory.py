"""Where a profiled trace's memory goes: tracemalloc's count of what Python allocates (the profile's `memory`).

Only for a trace that asks for it (a request's "memory", or the `profile_memory` setting): tracemalloc slows down
every allocation the process makes, a short request several times over, and the requests beside it too. It starts
with the first such trace, unless the app traces already, and stops once none has run for a while, even while other
traces are still profiled. It counts the whole process: a trace that ran beside others (`shared`) has their
allocations in its numbers too.

- `peak`: the highest level of Python's memory during the trace, above the level at its start. Exact while
  tracemalloc is ours: its peak is set back at each profiled trace's start, and the peak reached until then goes to
  the traces running. When the app traces itself, its peak is left alone: a trace's peak is the process's when it
  rose during the trace, else the highest level the sampler saw.
- `net`: what is still allocated at the end, minus what was at the start.
- `sites`: the allocations live at the peak (`sites_at`): the sampler takes a snapshot each time the trace's memory
  grew by a step over the last snapshot's, keeping the highest one, within a share of the trace's time. When none was
  near the peak, the picture at the end. A site is the project's frames down to the line that allocates, or down to
  the line that called the library that did, and that library's line.

Nothing counts all a trace allocated (`allocated`): tracemalloc keeps what is live, not a running total.
"""

import sys
import sysconfig
import threading
import time
import tracemalloc
from dataclasses import dataclass
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from types import CodeType, FunctionType, ModuleType

from tracepanel.core import forks
from tracepanel.core.frames import is_in_app

# Frames tracemalloc keeps of each allocation, innermost first: enough to reach the project's code from inside a
# library (an ORM's row, a template's node). Each one costs every allocation: the stack is walked that deep.
FRAMES = 16
# How much a trace's memory grows before the sampler takes another snapshot: a megabyte, or a tenth of the last one.
STEP = 1 << 20
# The share of a trace's time its snapshots may take (each copies every live allocation): no more are taken past it.
SNAPSHOT_SHARE = 0.25
# The sites a profile keeps, largest first.
MAX_SITES = 50
# Sampler ticks without a trace whose memory is counted before tracemalloc stops (it starts again with the next one).
IDLE_TICKS = 400
# This package's own allocations are its own business: made in its code, or in the standard library's on its behalf.
_OWN = str(Path(__file__).resolve().parents[1])
_STDLIB = tuple({str(Path(sysconfig.get_paths()[key]).resolve()) for key in ("stdlib", "platstdlib")})
# Files whose functions are kept by the lines they span, and lines looked up; past them the table starts again.
_MAX_FILES = 256
_MAX_PLACES = 20_000

# A frame of an allocation's traceback: its file and line.
Place = tuple[str, int]
# A site's frame as the profile's table names it: a function (code, module, line it runs, waiting), or a place in a
# file whose function isn't known (code that ran at a module's top level).
SiteKey = tuple[CodeType | None, str | None, int | None, bool] | Place
# A site: its frames, outermost first, the bytes live there and in how many blocks.
Site = tuple[list[SiteKey], int, int]
# A trace's memory without its sites (peak, net, sites_at), and its sites.
Measured = tuple[dict[str, object], list[Site]]


@dataclass(slots=True, eq=False)
class Memory:
    """One profiled trace's memory, as its start found it and the sampler saw it grow."""

    # tracemalloc is this package's: its peak is set back at the trace's start.
    owned: bool
    # Bytes traced at the start, and the highest level known since.
    start: int
    high: int
    # The process's peak at the start (an app's tracemalloc): a higher one at the end was reached during the trace.
    start_peak: int
    # When it began (perf_counter_ns): its snapshots may take a share of the time since.
    started: int
    baseline: tracemalloc.Snapshot | None = None
    # The highest snapshot taken during the trace, and its level above the start.
    picture: tracemalloc.Snapshot | None = None
    picture_level: int = 0
    snapshot_ns: int = 0

    def due(self, level: int, now: int) -> bool:
        """Whether a snapshot now would be a higher picture by a step, within the trace's share of snapshot time."""
        grown = level - self.start
        step = max(STEP, self.picture_level // 10)
        return grown >= self.picture_level + step and self.snapshot_ns <= SNAPSHOT_SHARE * (now - self.started)


class Tracer:
    """tracemalloc for the profiled traces running: started for them, its peak shared out between them."""

    def __init__(self) -> None:
        self._lock = forks.lock()
        self._live: list[Memory] = []
        # Whether this package started tracemalloc: it stops it again, and may set its peak back.
        self._owned = False
        # Sampler ticks since the last trace whose memory is counted ended.
        self._idle = 0

    def begin(self) -> Memory | None:
        """A trace's memory from now; tracemalloc starts if nothing traces yet. None if it couldn't trace."""
        with self._lock:
            self._idle = 0
            if not tracemalloc.is_tracing():
                tracemalloc.start(FRAMES)
                self._owned = True
        baseline = _snapshot()
        with self._lock:
            if baseline is None or not tracemalloc.is_tracing():
                return None
            level, peak = tracemalloc.get_traced_memory()
            if self._owned:
                # The peak so far is the running traces': it is set back for the new one.
                for memory in self._live:
                    memory.high = max(memory.high, peak)
                tracemalloc.reset_peak()
                peak = level
            memory = Memory(
                owned=self._owned,
                start=level,
                high=level,
                start_peak=peak,
                started=time.perf_counter_ns(),
                baseline=baseline,
            )
            self._live.append(memory)
        return memory

    def end(self, memory: Memory) -> tuple[int, int] | None:
        """The trace's highest level and its level now, in bytes; None if tracemalloc stopped meanwhile."""
        with self._lock:
            if memory in self._live:
                self._live.remove(memory)
            if not tracemalloc.is_tracing():
                return None
            level, peak = tracemalloc.get_traced_memory()
            if memory.owned or peak > memory.start_peak:
                memory.high = max(memory.high, peak)
            return max(memory.high, level), level

    def watch(self) -> None:
        """A sampler tick: each trace's highest level, and a snapshot for those that grew by a step since theirs.

        tracemalloc stops after IDLE_TICKS without a trace to count for, while time-only traces keep the sampler on.
        """
        if not self._live:
            if self._owned:
                self._idle += 1
                if self._idle >= IDLE_TICKS:
                    self.idle()
            return
        self._idle = 0
        now = time.perf_counter_ns()
        # Under the lock: a trace beginning hands the running ones the peak it sets back.
        with self._lock:
            if not tracemalloc.is_tracing():
                return
            level = tracemalloc.get_traced_memory()[0]
            due = [memory for memory in self._live if memory.due(level, now)]
            for memory in self._live:
                memory.high = max(memory.high, level)
        if not due:
            return
        snapshot = _snapshot()
        if snapshot is None:
            return
        took = time.perf_counter_ns() - now
        for memory in due:
            memory.picture, memory.picture_level = snapshot, level - memory.start
            memory.snapshot_ns += took

    def idle(self) -> None:
        """No trace whose memory is counted runs: tracemalloc stops, if this package started it."""
        with self._lock:
            if self._owned and not self._live:
                tracemalloc.stop()
                self._owned = False
                self._idle = 0


# One for the process: tracemalloc is the process's.
TRACER = Tracer()


def _snapshot() -> tracemalloc.Snapshot | None:
    try:
        return tracemalloc.take_snapshot()
    # The app stopped tracemalloc meanwhile.
    except RuntimeError:
        return None


def measure(memory: Memory, tracer: Tracer = TRACER) -> Measured | None:
    """The trace's `memory` without its sites, and the sites as frames to name: (frames, bytes, count), largest first.

    None if tracemalloc stopped during the trace.
    """
    levels = tracer.end(memory)
    if levels is None:
        return None
    high, level = levels
    peak = high - memory.start
    near = max(STEP, peak // 10)
    if memory.picture is not None and memory.picture_level >= peak - near:
        picture, at = memory.picture, "peak"
    else:
        picture = _snapshot()
        at = "peak" if level - memory.start >= peak - near else "end"
    sites = _sites_apart(memory.baseline, picture) if picture is not None else []
    memory.baseline = memory.picture = None
    return {"peak": peak, "net": level - memory.start, "sites_at": at}, sites


def _sites_apart(baseline: tracemalloc.Snapshot | None, picture: tracemalloc.Snapshot) -> list[Site]:
    """_sites(), in a thread of its own; none if it failed.

    Each allocation the work makes is traced too, its stack walked FRAMES deep: from a request's thread, deep in the
    server's and the framework's frames, that is every one of them; from a thread of its own, a few.
    """
    done: list[list[Site]] = []
    thread = threading.Thread(target=lambda: done.append(_sites(baseline, picture)), name="tracepanel-memory")
    try:
        thread.start()
    # The interpreter shuts down: no new thread.
    except RuntimeError:
        return _sites(baseline, picture)
    thread.join()
    return done[0] if done else []


def _sites(baseline: tracemalloc.Snapshot | None, picture: tracemalloc.Snapshot) -> list[Site]:
    """What the picture holds that the baseline didn't, by site: the largest MAX_SITES."""
    grown = _by_traceback(picture)
    if baseline is not None:
        for traceback, (size, count) in _by_traceback(baseline).items():
            now = grown.get(traceback)
            if now is not None:
                now[0] -= size
                now[1] -= count
    names = _Names()
    sites: dict[tuple[SiteKey, ...], list[int]] = {}
    for traceback, (size, count) in grown.items():
        if size <= 0 or not traceback:
            continue
        keys = names.site(traceback)
        if keys is None:
            continue
        found = sites.setdefault(keys, [0, 0])
        found[0] += size
        found[1] += max(count, 0)
    largest = sorted(sites.items(), key=lambda item: -item[1][0])[:MAX_SITES]
    return [(list(keys), size, count) for keys, (size, count) in largest]


def _by_traceback(snapshot: tracemalloc.Snapshot) -> dict[tuple[Place, ...], list[int]]:
    """Bytes and blocks live by traceback (innermost frame first, as tracemalloc keeps them).

    Each allocation made meanwhile is traced too, its stack walked: the work is done by sorting and summing in C, one
    list per traceback rather than objects for each of the many blocks.
    """
    # The raw (domain, size, traceback, frames) tuples: Trace objects made for each would cost several times more.
    raw = getattr(snapshot.traces, "_traces", None)
    if not isinstance(raw, list):
        raw = [
            (0, trace.size, tuple((frame.filename, frame.lineno) for frame in reversed(trace.traceback)), 0)
            for trace in snapshot.traces
        ]
    grouped: dict[tuple[Place, ...], list[int]] = {}
    for traceback, entries in groupby(sorted(raw, key=_TRACEBACK), key=_TRACEBACK):
        sizes = list(map(_SIZE, entries))
        grouped[traceback] = [sum(sizes), len(sizes)]
    return grouped


_SIZE = itemgetter(1)
_TRACEBACK = itemgetter(2)


class _Names:
    """An allocation's traceback as a site: the profile's frames for the places it names."""

    def __init__(self) -> None:
        self._places: dict[Place, tuple[list[CodeType], str | None, bool]] = {}

    def site(self, traceback: tuple[Place, ...]) -> tuple[SiteKey, ...] | None:
        """The project's frames, outermost first, the innermost at its line; the library's line it called, if any.

        None for an allocation of this package's own.
        """
        if _own(traceback):
            return None
        named: list[tuple[Place, CodeType | None, str | None, bool]] = []
        inner: CodeType | None = None
        for place in traceback:
            functions, module, in_app = self._name(place)
            inner = _function(functions, inner)
            named.append((place, inner, module, in_app))
        named.reverse()
        in_app = [index for index, (_, _, _, app) in enumerate(named) if app]
        keys: list[SiteKey] = []
        for index in in_app[:-1]:
            _, code, module, _ = named[index]
            if code is not None:
                keys.append((code, module, None, False))
        if in_app:
            keys.append(_at(*named[in_app[-1]][:3]))
        if not named[-1][3]:
            keys.append(_at(*named[-1][:3]))
        return tuple(keys)

    def _name(self, place: Place) -> tuple[list[CodeType], str | None, bool]:
        found = self._places.get(place)
        if found is None:
            filename, line = place
            module, functions = _CODE.at(filename, line)
            found = self._places[place] = (functions, module, is_in_app(filename, module or ""))
        return found


def _function(functions: list[CodeType], inner: CodeType | None) -> CodeType | None:
    """Which of the functions a line is in (outermost first) a frame runs, given the function of the frame it called.

    A frame on the line a comprehension or lambda shares with the function it is in, calling it: that function.
    """
    if not functions:
        return None
    if inner in functions:
        position = functions.index(inner)
        return functions[position - 1] if position else inner
    return functions[-1]


def _own(traceback: tuple[Place, ...]) -> bool:
    """Whether this package made the allocation: the innermost frame outside the standard library is its own."""
    for filename, _ in traceback:
        if not filename.startswith(_STDLIB):
            return filename.startswith(_OWN)
    return False


def _at(place: Place, code: CodeType | None, module: str | None) -> SiteKey:
    """A frame at the line it runs: its function's, or the place itself when no function has the line."""
    return (code, module, place[1], False) if code is not None else place


class _Code:
    """Which functions a file's line is in: the modules by their file, each file's functions by the lines they span."""

    def __init__(self) -> None:
        self._modules: dict[str, ModuleType] = {}
        # How many modules there were when they were listed: a new one lists them again.
        self._listed = 0
        # Each file's functions, (first line, last line, code), the outermost first.
        self._spans: dict[str, list[tuple[int, int, CodeType]]] = {}
        self._found: dict[Place, list[CodeType]] = {}

    def at(self, filename: str, line: int) -> tuple[str | None, list[CodeType]]:
        """The module a file is, and the functions its line is in, outermost first; none for what's in neither."""
        module = self._module(filename)
        if module is None:
            return None, []
        found = self._found.get((filename, line))
        if found is None:
            spans = self._spans.get(filename)
            if spans is None:
                if len(self._spans) >= _MAX_FILES:
                    self._spans.clear()
                spans = self._spans[filename] = _spans(_functions(module, filename))
            if len(self._found) >= _MAX_PLACES:
                self._found.clear()
            found = self._found[filename, line] = [code for first, last, code in spans if first <= line <= last]
        return module.__name__, found

    def _module(self, filename: str) -> ModuleType | None:
        if len(sys.modules) != self._listed:
            self._modules.clear()
            for module in list(sys.modules.values()):
                file = getattr(module, "__file__", None)
                if isinstance(file, str):
                    self._modules[file] = module
            self._listed = len(sys.modules)
        return self._modules.get(filename)


_CODE = _Code()


def _functions(module: ModuleType, filename: str) -> list[CodeType]:
    """The code of the functions in a module's file that its namespace and classes reach."""
    seen: set[int] = set()
    codes: list[CodeType] = []
    pending: list[object] = list(vars(module).values())
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, (staticmethod, classmethod)):
            pending.append(value.__func__)
        elif isinstance(value, property):
            pending.extend((value.fget, value.fset, value.fdel))
        elif isinstance(value, FunctionType):
            pending.append(getattr(value, "__wrapped__", None))
            if value.__code__.co_filename == filename:
                codes.append(value.__code__)
        elif isinstance(value, type) and value.__module__ == module.__name__:
            pending.extend(vars(value).values())
    return codes


def _spans(codes: list[CodeType]) -> list[tuple[int, int, CodeType]]:
    """The lines each function and those defined in it span, the widest first: one inside another comes after it."""
    spans: list[tuple[int, int, CodeType]] = []
    known = {id(code) for code in codes}
    while codes:
        code = codes.pop()
        lines = [number for _, _, number in code.co_lines() if number is not None]
        if lines:
            spans.append((min(lines), max(lines), code))
        for const in code.co_consts:
            if isinstance(const, CodeType) and id(const) not in known:
                known.add(id(const))
                codes.append(const)
    spans.sort(key=lambda span: span[0] - span[1])
    return spans
