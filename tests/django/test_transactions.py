import pytest
from dirty_equals import IsStr
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.test import Client
from pytest_django.fixtures import Settings

from tracepanel.core.model import Span, Trace
from tracepanel.django import services as django_services


def shape(trace: Trace, kinds: set[str]) -> list[tuple[str, str, str | None]]:
    """(kind, name, parent name) of the spans of these kinds."""
    by_id = {span.id: span for span in trace.spans}
    return [
        (span.kind, span.name, by_id[span.parent_id].name if span.parent_id in by_id else None)
        for span in trace.spans
        if span.kind in kinds
    ]


def transactions(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "transaction"]


@pytest.mark.django_db(transaction=True)
def test_atomic_blocks_and_on_commit(client: Client, traces: list[Trace]) -> None:
    client.get("/place-order/")
    (trace,) = traces
    outer, savepoint, failed = transactions(trace)
    callback = next(span for span in trace.spans if span.name.startswith("on_commit"))

    assert shape(trace, {"transaction", "mail"}) == [
        ("transaction", "transaction default", "tests.project.views.place_order"),
        ("transaction", "savepoint default", "transaction default"),
        ("mail", "mail Order placed", "transaction default"),
        ("mail", "mail Your receipt", "on_commit tests.project.views.send_receipt"),
        ("transaction", "transaction default", "tests.project.views.place_order"),
    ]
    assert outer.attrs == {
        "db.alias": "default",
        **django_services.for_connection(connection),
        "db.transaction.savepoint": False,
        "db.transaction.durable": False,
        "code.origin": IsStr(regex=r"tests/project/views\.py:\d+ in place_order"),
        "db.transaction.outcome": "commit",
    }
    assert (savepoint.attrs["db.transaction.savepoint"], savepoint.attrs["db.transaction.outcome"]) == (True, "commit")
    assert (failed.status, failed.attrs["db.transaction.outcome"]) == ("error", "rollback")
    # The callback ran after COMMIT: next to the transaction, not inside it, and not in its time.
    assert (callback.parent_id, callback.attrs["db.transaction.on_commit"]) == (outer.parent_id, True)
    assert outer.start + outer.duration <= callback.start


@pytest.mark.django_db(transaction=True)
def test_work_that_belongs_after_commit(client: Client, traces: list[Trace]) -> None:
    client.get("/place-order/")
    (trace,) = traces

    assert [(issue.kind, issue.title, issue.culprit) for issue in trace.issues] == [
        (
            "in-transaction",
            "Email sent inside a transaction · mail Order placed",
            IsStr(regex=r"tests/project/views\.py:\d+ in place_order"),
        )
    ]


@pytest.mark.django_db(transaction=True)
def test_long_transaction(client: Client, traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"long_transaction_ms": 0}

    client.get("/place-order/")
    (trace,) = traces

    assert sorted(issue.kind for issue in trace.issues) == ["in-transaction", "long-transaction", "long-transaction"]
    assert [issue.title for issue in trace.issues if issue.kind == "long-transaction"] == [
        IsStr(regex=r"Long transaction: \d+ ms on default")
    ] * 2


@pytest.mark.django_db
def test_outside_a_trace_atomic_is_untouched() -> None:
    with transaction.atomic():
        Group.objects.create(name="quiet")

    assert Group.objects.filter(name="quiet").exists()
