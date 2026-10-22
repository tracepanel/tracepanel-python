import pytest

from tracepanel.core import profile
from tracepanel.core.context import current_span, current_trace, start_trace


class Stopped(BaseException):
    """What a signal handler or another thread raises in the code it stops: anywhere, a time limit say."""


def stopped(*, asked: bool) -> bool:
    del asked
    raise Stopped


def test_a_trace_stopped_as_it_begins_leaves_the_context_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    before = (current_trace(), current_span(), profile.current.get())
    with start_trace("command", "outer") as outer:
        # Profiling is decided once the trace is current: stopped there, it was current already.
        monkeypatch.setattr(profile, "wanted", stopped)
        with pytest.raises(Stopped), start_trace("task", "inner"):
            pass

        assert (current_trace(), current_span()) == (outer, outer.spans[0])

    assert (current_trace(), current_span(), profile.current.get()) == before


def test_a_trace_can_be_ended_again_and_one_never_begun_ends_as_a_no_op() -> None:
    before = current_trace()
    block = start_trace("task", "run")
    block.__exit__(None, None, None)
    trace = block.__enter__()
    block.__exit__(None, None, None)
    duration = trace.duration
    block.__exit__(Stopped, Stopped(), None)

    assert (current_trace(), trace.duration, trace.status) == (before, duration, "ok")
