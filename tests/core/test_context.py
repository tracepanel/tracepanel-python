from tracepanel.core.context import current_span, current_trace, open_span, start_span, start_trace


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
