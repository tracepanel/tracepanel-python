"""Reading a trace's profile in tests."""

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
