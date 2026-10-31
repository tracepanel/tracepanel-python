import copy
import gc
import threading
import tracemalloc
from collections.abc import Generator

import pytest
from dirty_equals import IsInt, IsList, IsPartialDict, IsStr

from tests.profiles import busy
from tracepanel.core import conf, memory
from tracepanel.core.context import start_trace
from tracepanel.core.model import Trace
from tracepanel.core.profile import MEMORY

# A list of 5 million items: 40 MB of pointers.
COUNT = 5_000_000
SIZE = 8 * COUNT
# A trace's numbers with the list in them: tracemalloc counts the whole process, and its other threads (the sampler,
# pytest-xdist's) free a little of what they had before the trace meanwhile, far under a megabyte.
HELD = SIZE - (1 << 20)


@pytest.fixture(autouse=True)
def often(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()


@pytest.fixture
def untraced() -> Generator[None, None, None]:
    """No tracemalloc left from an earlier test's profiled traces: it stops as it does once the sampler is idle."""
    memory.TRACER.idle()
    assert not tracemalloc.is_tracing()
    yield
    if tracemalloc.is_tracing():
        tracemalloc.stop()


def profile_of(trace: Trace) -> dict[str, object]:
    assert trace.profile is not None
    return trace.profile


def memory_of(trace: Trace) -> dict[str, object]:
    found = profile_of(trace)["memory"]
    assert isinstance(found, dict)
    return found


def frames_of(trace: Trace, site: dict[str, object]) -> list[dict[str, object]]:
    """A site's frames, outermost first."""
    frames = profile_of(trace)["frames"]
    indexes = site["frames"]
    assert isinstance(frames, list)
    assert isinstance(indexes, list)
    return [frames[index] for index in indexes]


def largest(trace: Trace) -> dict[str, object]:
    sites = memory_of(trace)["sites"]
    assert isinstance(sites, list)
    return sites[0]


def hold() -> list[int]:
    numbers = [0] * COUNT
    busy(0.05)
    return numbers


def test_a_profiled_trace_has_its_peak_and_where_the_memory_went() -> None:
    with start_trace("command", "build", profile=MEMORY) as trace:
        numbers = hold()
    del numbers
    line = hold.__code__.co_firstlineno

    assert memory_of(trace) == {
        "peak": IsInt(ge=HELD),
        "net": IsInt(ge=HELD),
        "sites_at": "peak",
        "sites": IsList(length=(1, 50)),
        "shared": False,
    }

    assert largest(trace) == {"frames": IsList(length=(1, ...)), "bytes": IsInt(ge=SIZE), "count": IsInt(ge=1)}
    # The project's frames, down to the line that allocates.
    assert frames_of(trace, largest(trace))[-1] == {
        "function": "hold",
        "module": "tests.core.test_memory",
        "file": "tests/core/test_memory.py",
        "line": line,
        "at_line": line + 1,
        "in_app": True,
    }
    assert all(frame["in_app"] for frame in frames_of(trace, largest(trace)))


def test_memory_freed_before_the_end_is_in_the_peak_not_the_net() -> None:
    with start_trace("command", "build", profile=MEMORY) as trace:
        hold()
        busy(0.02)

    assert memory_of(trace) == {
        "peak": IsInt(ge=HELD),
        "net": IsInt(lt=SIZE // 100),
        "sites_at": "peak",
        "sites": IsList(length=(1, 50)),
        "shared": False,
    }
    # The sampler's picture, taken while the list was there.
    assert largest(trace) == {"frames": IsList(length=(1, ...)), "bytes": IsInt(ge=SIZE), "count": IsInt(ge=1)}
    assert frames_of(trace, largest(trace))[-1]["function"] == "hold"


def copied() -> list[dict[str, int]]:
    rows = [{"id": number} for number in range(50_000)]
    copies = copy.deepcopy(rows)
    busy(0.05)
    return copies


def test_a_library_that_allocates_is_named_after_the_projects_line_that_called_it() -> None:
    with start_trace("command", "copy", profile=MEMORY) as trace:
        copies = copied()
    line = copied.__code__.co_firstlineno
    sites = memory_of(trace)["sites"]
    assert isinstance(sites, list)
    places = [frames_of(trace, site)[-2:] for site in sites]

    assert len(copies) == 50_000
    # The project's line that called copy.deepcopy, then where in copy the memory was allocated.
    assert [
        {
            "function": "copied",
            "module": "tests.core.test_memory",
            "file": "tests/core/test_memory.py",
            "line": line,
            "at_line": line + 2,
            "in_app": True,
        },
        IsPartialDict({"function": IsStr(), "module": "copy", "in_app": False, "at_line": IsInt()}),
    ] in places
    # The rows: the project's own line.
    assert any(frames[-1].get("at_line") == line + 1 and frames[-1]["in_app"] for frames in places)


def test_an_unprofiled_trace_has_no_memory_or_gc() -> None:
    with start_trace("command", "plain") as trace:
        hold()
        gc.collect()

    assert trace.profile is None
    assert "profile" not in trace.to_dict()


@pytest.mark.usefixtures("untraced")
def test_a_profiled_trace_counts_memory_only_when_asked() -> None:
    with start_trace("command", "build", profile=True) as trace:
        hold()
        gc.collect()
    tracing = tracemalloc.is_tracing()

    assert profile_of(trace) == IsPartialDict(
        {
            "samples": IsInt(gt=0),
            "gc": {"collections": IsInt(ge=1), "pause_us": IsInt(ge=0), "pauses": IsList(length=(1, ...))},
        }
    )
    assert "memory" not in profile_of(trace)

    assert not tracing


@pytest.mark.usefixtures("untraced")
def test_the_setting_counts_every_profiled_traces_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_MEMORY", "1")
    conf.reset()

    with start_trace("command", "build", profile=True) as trace:
        numbers = hold()
    with start_trace("command", "plain") as plain:
        hold()
    del numbers

    assert memory_of(trace) == IsPartialDict({"peak": IsInt(ge=HELD), "net": IsInt(ge=HELD)})

    assert plain.profile is None


@pytest.mark.usefixtures("untraced")
def test_tracemalloc_stops_while_only_time_is_profiled() -> None:
    # A tracer of its own, ticked here alone: the process's is ticked by the sampler thread too, which goes on for a
    # while after the last profiled trace, and would count ticks of its own between these.
    tracer = memory.Tracer()
    counted = tracer.begin()
    assert counted is not None
    tracer.end(counted)
    started = tracemalloc.is_tracing()
    # The sampler ticks on for a trace profiled for its time alone.
    for _ in range(memory.IDLE_TICKS - 1):
        tracer.watch()
    still = tracemalloc.is_tracing()
    tracer.watch()

    assert started
    assert still
    assert not tracemalloc.is_tracing()


@pytest.mark.usefixtures("untraced")
def test_a_sampler_leaving_while_a_trace_begins_leaves_its_tracemalloc_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sampler thread idle long enough leaves (idle()) as a trace's memory begins: tracemalloc has started for that
    # trace, which takes its baseline snapshot before it is counted as running.
    tracer = memory.Tracer()
    snapshot = memory._snapshot  # noqa: SLF001

    def leaving_meanwhile() -> tracemalloc.Snapshot | None:
        tracer.idle()
        return snapshot()

    monkeypatch.setattr(memory, "_snapshot", leaving_meanwhile)
    counted = tracer.begin()
    tracing = tracemalloc.is_tracing()
    monkeypatch.undo()

    assert counted is not None
    assert tracing

    # Ended, the trace leaves it to the next idle() to stop.
    tracer.end(counted)
    tracer.idle()

    assert not tracemalloc.is_tracing()


def test_garbage_collections_during_a_profiled_trace_are_its_pauses() -> None:
    with start_trace("command", "collects", profile=True) as trace:
        busy(0.01)
        gc.collect()
        busy(0.01)
    pauses = profile_of(trace)["gc"]

    assert pauses == {
        "collections": IsInt(ge=1),
        "pause_us": IsInt(ge=0),
        "pauses": IsList(length=(1, ...)),
    }
    assert isinstance(pauses, dict)
    # The full collection, after the first 10 ms of the trace.
    assert [IsInt(ge=9_000), IsInt(ge=0), 2] in pauses["pauses"]
    assert sum(pause[1] for pause in pauses["pauses"]) <= pauses["pause_us"]


def keep() -> list[bytearray]:
    return [bytearray(1000) for _ in range(10)]


@pytest.mark.usefixtures("untraced")
def test_an_app_tracing_memory_itself_is_left_as_it_was() -> None:
    tracemalloc.start(5)
    kept = keep()
    spike = bytearray(4 * SIZE)
    del spike
    _, peak_before = tracemalloc.get_traced_memory()

    with start_trace("command", "build", profile=MEMORY) as trace:
        numbers = hold()
    # The sampler, idle for a while.
    memory.TRACER.idle()
    tracing, limit = tracemalloc.is_tracing(), tracemalloc.get_traceback_limit()
    _, peak_after = tracemalloc.get_traced_memory()
    still_kept = tracemalloc.take_snapshot().statistics("lineno")
    tracemalloc.stop()
    del numbers

    assert tracing
    assert limit == 5
    # Its peak isn't set back: the app's own, from before the trace, is still the highest.
    assert peak_after >= peak_before
    assert any(stat.traceback[0].lineno == keep.__code__.co_firstlineno + 1 for stat in still_kept)
    assert len(kept) == 10

    # The trace's peak is the highest level the sampler saw.
    assert memory_of(trace) == {
        "peak": IsInt(ge=HELD),
        "net": IsInt(ge=HELD),
        "sites_at": "peak",
        "sites": IsList(length=(1, 50)),
        "shared": False,
    }


def test_a_trace_beside_others_says_its_numbers_are_shared() -> None:
    with start_trace("command", "alone", profile=MEMORY) as alone:
        busy(0.02)
    begun, done = threading.Event(), threading.Event()
    results: list[Trace] = []

    def plain() -> None:
        with start_trace("command", "plain"):
            begun.set()
            done.wait(5)

    def profiled(barrier: threading.Barrier) -> None:
        with start_trace("command", "profiled", profile=MEMORY) as trace:
            barrier.wait(5)
            busy(0.1)
            barrier.wait(5)
        results.append(trace)

    # An unprofiled trace begun before the profiled one, still running.
    other = threading.Thread(target=plain)
    other.start()
    begun.wait(5)
    with start_trace("command", "beside", profile=MEMORY) as beside:
        busy(0.02)
    done.set()
    other.join()
    # Two profiled traces at once.
    barrier = threading.Barrier(2)
    both = [threading.Thread(target=profiled, args=(barrier,)) for _ in range(2)]
    for thread in both:
        thread.start()
    for thread in both:
        thread.join()

    assert memory_of(alone)["shared"] is False
    assert memory_of(beside)["shared"] is True
    assert [memory_of(trace)["shared"] for trace in results] == [True, True]
