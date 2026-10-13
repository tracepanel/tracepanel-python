import pytest
from django.contrib.auth.models import Group

from tests.agentstore import issues
from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.integrations.dbapi import _core
from tracepanel.storage.transport import complete

pytestmark = pytest.mark.django_db


def rows(trace: Trace) -> list[object]:
    return [span.attrs.get("db.response.returned_rows") for span in trace.spans if span.kind == "sql"]


def test_the_rows_a_query_fetched_are_counted() -> None:
    Group.objects.bulk_create([Group(name=f"group {n}") for n in range(250)])

    with start_trace("http", "GET /groups") as trace:
        list(Group.objects.all())
        Group.objects.order_by("id").first()
        # Fetched 100 at a time: every chunk counts.
        list(Group.objects.iterator(chunk_size=100))
        # Nothing found: none.
        list(Group.objects.filter(name="none"))

    assert rows(complete(trace)) == [250, 1, 250, 0]


def test_the_agent_finds_a_select_that_read_a_whole_table() -> None:
    Group.objects.bulk_create([Group(name=f"group {n}") for n in range(1200)])

    with start_trace("http", "GET /export") as trace:
        list(Group.objects.all())
        # Bounded: as many rows as asked for, however many.
        list(Group.objects.all()[:1100])

    assert [(issue.kind, issue.title) for issue in issues(complete(trace))] == [
        ("large-result", "Large result: 1200 rows · SELECT auth_group"),
    ]


def test_a_driver_that_says_how_many_rows_a_select_found() -> None:
    class Cursor:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

    def span(statement: str) -> Span:
        return Span(id="S", parent_id=None, kind="sql", name="SELECT", start=0, attrs={"db.statement": statement})

    found, unknown, written = span("SELECT * FROM item"), span("SELECT * FROM item"), span("UPDATE item SET x = 1")
    _core._rowcount(Cursor(42), found)  # noqa: SLF001
    # SQLite's cursor says -1 for a SELECT; an UPDATE's count is rows changed, not returned.
    _core._rowcount(Cursor(-1), unknown)  # noqa: SLF001
    _core._rowcount(Cursor(7), written)  # noqa: SLF001

    assert [item.attrs.get("db.response.returned_rows") for item in (found, unknown, written)] == [42, None, None]
