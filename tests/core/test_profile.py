import sys
import threading
import time
from pathlib import Path
from types import FrameType

import django
import pytest
from dirty_equals import IsInt

from tracepanel.core import conf, frames, profile
from tracepanel.core.context import start_trace
from tracepanel.core.profile import MAX_STACKS, OTHER, Recording


def outer() -> FrameType:
    return inner()


def inner() -> FrameType:
    frame = sys._getframe()  # noqa: SLF001
    assert frame is not None
    return frame


def test_a_stack_is_read_outermost_first_by_function() -> None:
    keys = profile.stack(outer())

    assert [code.co_qualname for code, _ in keys[-2:] if code is not None] == ["outer", "inner"]
    assert keys[-1][1] == "tests.core.test_profile"


def test_the_profile_names_each_function_once_and_counts_each_stack() -> None:
    a = (outer.__code__, "tests.core.test_profile")
    b = (inner.__code__, "tests.core.test_profile")
    recording = Recording(thread_id=1, interval_us=5000)
    profile.count(recording, (a, b))
    profile.count(recording, (a, b))
    profile.count(recording, (a,))

    assert profile.document(recording) == {
        "interval_us": 5000,
        "samples": 3,
        "frames": [
            {
                "function": "outer",
                "module": "tests.core.test_profile",
                "file": "tests/core/test_profile.py",
                "line": IsInt(),
                "in_app": True,
            },
            {
                "function": "inner",
                "module": "tests.core.test_profile",
                "file": "tests/core/test_profile.py",
                "line": IsInt(),
                "in_app": True,
            },
        ],
        "stacks": [{"frames": [0, 1], "count": 2}, {"frames": [0], "count": 1}],
    }


def test_past_the_cap_new_stacks_count_as_other() -> None:
    recording = Recording(thread_id=1, interval_us=5000)
    for depth in range(MAX_STACKS + 3):
        profile.count(recording, tuple((outer.__code__, f"m{level}") for level in range(depth + 1)))

    assert len(recording.stacks) == MAX_STACKS + 1
    assert recording.stacks[(OTHER,)] == 3
    assert recording.samples == MAX_STACKS + 3


def test_nothing_sampled_is_no_profile() -> None:
    assert profile.document(Recording(thread_id=1, interval_us=5000)) is None


def test_a_request_asks_by_header_or_by_the_panels_cookie() -> None:
    assert profile.requested("1", None)
    assert profile.requested(None, "theme=dark; tracepanel_profile=1")
    assert profile.by_cookie("tracepanel_profile=1")

    assert not profile.requested(None, None)
    assert not profile.requested("0", "tracepanel_profile=0")
    assert not profile.by_cookie("not_tracepanel_profile=1; =broken")


def test_profiling_follows_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert profile.wanted(asked=True)
    assert not profile.wanted(asked=False)

    monkeypatch.setenv("TRACEPANEL_PROFILE", "1")
    conf.reset()
    assert profile.wanted(asked=False)

    monkeypatch.setenv("TRACEPANEL_PROFILE_ENABLED", "0")
    conf.reset()
    assert not profile.wanted(asked=True)


def spin() -> None:
    event = threading.Event()
    event.wait(0.08)
    total = 0
    for number in range(400_000):
        total += number


def test_a_profiled_trace_carries_its_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()

    with start_trace("command", "spin", profile=True) as trace:
        spin()
    with start_trace("command", "spin") as unprofiled:
        spin()

    assert trace.profile is not None
    assert trace.profile["samples"] == IsInt(gt=0)
    assert "spin" in in_app(trace.profile)
    assert trace.to_dict()["profile"] == trace.profile

    assert unprofiled.profile is None
    assert "profile" not in unprofiled.to_dict()


def in_app(document: dict[str, object]) -> set[str]:
    """The project's functions a profile names."""
    frames = document["frames"]
    assert isinstance(frames, list)
    return {str(frame["function"]) for frame in frames if isinstance(frame, dict) and frame["in_app"]}


def test_a_library_frame_is_named_by_its_package_path() -> None:
    handlers = str(Path(django.__file__).parent / "core" / "handlers" / "base.py")

    assert frames.library_path(handlers) == "django/core/handlers/base.py"


def test_a_trace_starting_as_the_idle_sampler_leaves_is_still_sampled(monkeypatch: pytest.MonkeyPatch) -> None:
    # The sampler leaves after two idle ticks: traces a tick or two apart start just as it goes.
    monkeypatch.setattr(profile, "IDLE_TICKS", 2)
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()
    sampler = profile.Sampler()
    missed = 0
    # Long enough that the OS gives the sampler a turn whatever else runs: only the race could leave it out.
    for gap in [0.0, 0.001, 0.002, 0.003] * 5:
        time.sleep(gap)
        recording = sampler.start()
        busy(0.1)
        if sampler.stop(recording) is None:
            missed += 1

    assert missed == 0


def test_while_traces_are_profiled_the_gil_changes_hands_as_often_as_the_sampler_wakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()
    before = sys.getswitchinterval()
    sampler = profile.Sampler()

    recording = sampler.start()
    during = sys.getswitchinterval()
    sampler.stop(recording)

    assert during == 0.001
    assert sys.getswitchinterval() == before


def busy(seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass
