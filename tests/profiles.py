"""Reading a trace's profile in tests."""

import time

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


def busy(seconds: float = 0.05) -> int:
    """Busy for a while: a run long enough for the profiler to sample it, when it is profiled."""
    end = time.perf_counter() + seconds
    turns = 0
    while time.perf_counter() < end:
        turns += 1
    return turns
