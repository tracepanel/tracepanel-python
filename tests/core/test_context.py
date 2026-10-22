import pytest

from tracepanel.core import profile
from tracepanel.core.context import current_span, current_trace, open_span, start_span, start_trace


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


def test_a_trace_ended_inside_a_span_of_its_own_leaves_nothing_current() -> None:
    # A task's trace begun in one signal's receiver and ended in another's, inside that send's span.
    context = start_trace("task", "send_report", root_kind="task.run")
    context.__enter__()
    with start_span("signal", "task_finished"):
        context.__exit__(None, None, None)

    assert (current_trace(), current_span()) == (None, None)


def test_a_trace_begun_inside_a_span_of_another_records_under_its_own_root() -> None:
    with start_trace("http", "GET /reports/") as request:
        with start_span("task.enqueue", "enqueue send_report") as enqueue:
            context = start_trace("task", "send_report", root_kind="task.run")
            with start_span("signal", "task_started"):
                run = context.__enter__()
            with start_span("sql", "SELECT"):
                pass
            with start_span("signal", "task_finished"):
                context.__exit__(None, None, None)

            assert (current_trace(), current_span()) == (request, enqueue)

        assert (current_trace(), current_span()) == (request, request.spans[0])

    assert [(span.kind, span.parent_id) for span in run.spans] == [
        ("task.run", None),
        ("sql", run.spans[0].id),
        ("signal", run.spans[0].id),
    ]

    assert (current_trace(), current_span()) == (None, None)


def test_an_open_span_ended_while_a_trace_begun_inside_it_runs() -> None:
    with start_trace("http", "GET /") as request:
        transaction = open_span("transaction", "BEGIN")
        context = start_trace("task", "send_report")
        context.__enter__()
        assert transaction is not None
        transaction.end()
        context.__exit__(None, None, None)

        assert (current_trace(), current_span()) == (request, request.spans[0])
