import sys
import threading
import time
from pathlib import Path
from types import FrameType

import django
import pytest
from dirty_equals import IsInt

from tests.profiles import share
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


def carried() -> dict[str, object]:
    """What a message sent now carries."""
    headers: dict[str, object] = {}
    profile.carry(headers, "tracepanel_profile")
    return headers


def test_work_a_profiled_trace_hands_off_asks_to_be_profiled_too() -> None:
    with start_trace("http", "POST /reports", profile=True):
        asked = carried()
    with start_trace("http", "GET /"):
        plain = carried()

    assert asked == {"tracepanel_profile": "1"}
    assert plain == {}
    assert carried() == {}


def test_a_trace_the_setting_profiles_hands_nothing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every trace is profiled anyway: the worker's setting decides for its runs.
    monkeypatch.setenv("TRACEPANEL_PROFILE", "1")
    conf.reset()

    with start_trace("http", "GET /"):
        assert carried() == {}


def test_nothing_is_handed_on_while_profiling_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_ENABLED", "0")
    conf.reset()

    with start_trace("http", "POST /reports", profile=True):
        assert carried() == {}


def test_a_run_asks_when_its_work_carries_the_flag_unless_it_runs_inside_a_profiled_trace() -> None:
    assert profile.asked_by_parent("1")
    assert profile.asked_by_parent(b"1")

    assert not profile.asked_by_parent(None)
    assert not profile.asked_by_parent("0")
    assert not profile.asked_by_parent(1)

    # An eager run, inside its caller: the caller's profile has its frames.
    with start_trace("http", "POST /reports", profile=True):
        assert not profile.asked_by_parent("1")
    with start_trace("http", "GET /"):
        assert profile.asked_by_parent("1")


def test_a_run_that_followed_its_parent_hands_the_request_on_again() -> None:
    with start_trace("task", "build_report", profile=profile.asked_by_parent("1")):
        sent = carried()

    assert sent == {"tracepanel_profile": "1"}


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


def test_work_handed_to_another_thread_is_sampled_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()

    with start_trace("command", "hop", profile=True) as trace:
        worker = threading.Thread(target=profile.followed(busy), args=(0.3,))
        worker.start()
        worker.join()

    assert trace.profile is not None
    assert "busy" in in_app(trace.profile)
    # While the work is away, the trace's own thread only waits for it: its join is not what the profile shows. Not
    # followed, `busy` would have no samples at all; a shared CI runner loses some of them to the thread switch.
    assert share(trace, "busy") > 0.3


def test_outside_a_profiled_trace_a_call_is_left_as_it_is() -> None:
    assert profile.followed(busy) is busy

    with start_trace("command", "plain"):
        assert profile.followed(busy) is busy


def test_traces_sharing_a_thread_are_each_sampled_where_their_work_went(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two requests on one event loop: neither can be told apart there, each can in its worker thread.
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()
    sampler = profile.Sampler()
    first, second = sampler.start(), sampler.start()

    def work(recording: Recording) -> None:
        with sampler.follow(recording):
            busy(0.15)

    workers = [threading.Thread(target=work, args=(recording,)) for recording in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    documents = [sampler.stop(first), sampler.stop(second)]

    for document in documents:
        assert document is not None
        assert {"busy", "test_traces_sharing_a_thread_are_each_sampled_where_their_work_went.<locals>.work"} <= in_app(
            document
        )


def test_work_coming_back_to_the_traces_own_thread_is_sampled_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()
    sampler = profile.Sampler()
    recording = sampler.start()

    with sampler.follow(recording):
        busy(0.1)
    document = sampler.stop(recording)

    assert document is not None
    assert "busy" in in_app(document)
    assert recording.away == 0
