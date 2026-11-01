"""Reading a trace's profile in tests."""

import time

from tracepanel.core import profile
from tracepanel.core.model import Trace


def share(trace: Trace, function: str) -> float:
    """The share of the trace's samples whose stack runs through the function (by its qualified name)."""
    assert trace.profile is not None
    frames = trace.profile["frames"]
    stacks = trace.profile["stacks"]
    assert isinstance(frames, list)
    assert isinstance(stacks, list)
    names = [frame["function"] for frame in frames]
    inside = sum(stack["count"] for stack in stacks if any(names[index] == function for index in stack["frames"]))
    return inside / sum(stack["count"] for stack in stacks)


def share_meanwhile(trace: Trace, function: str) -> float:
    """The share running through the function of the samples seen while it ran: between its first and last stretch.

    Unlike share(), what the trace did before and after (waiting for a thread under load) doesn't count.
    """
    assert trace.profile is not None
    frames = trace.profile["frames"]
    stacks = trace.profile["stacks"]
    assert isinstance(frames, list)
    assert isinstance(stacks, list)
    names = [frame["function"] for frame in frames]
    through = [any(names[index] == function for index in stack["frames"]) for stack in stacks]
    buckets = [bucket for stack, inside in zip(stacks, through, strict=True) if inside for bucket, _ in stack["at"]]
    first, last = min(buckets), max(buckets)
    counts = [
        (inside, sum(count for bucket, count in stack["at"] if first < bucket < last))
        for stack, inside in zip(stacks, through, strict=True)
    ]
    return sum(count for inside, count in counts if inside) / sum(count for _, count in counts)


def busy(seconds: float = 0.05) -> int:
    """Busy for a while: a run long enough for the profiler to sample it, when it is profiled."""
    end = time.perf_counter() + seconds
    turns = 0
    while time.perf_counter() < end:
        turns += 1
    return turns


def sampled(seconds: float, *, samples: int = 20) -> int:
    """Busy for a while, and until the profiler has sampled the current trace that many times since.

    However long a loaded machine keeps the sampler waiting. Just busy outside a profiled trace.
    """
    recording = profile.current.get()
    start = recording.taken if recording is not None else 0
    end = time.perf_counter() + seconds
    deadline = end + 10
    turns = 0
    while time.perf_counter() < end or (
        recording is not None and recording.taken - start < samples and time.perf_counter() < deadline
    ):
        turns += 1
    return turns
