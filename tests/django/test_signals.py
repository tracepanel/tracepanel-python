import pytest
from dirty_equals import IsPositiveInt
from django.contrib.auth.models import Group
from django.db.models.signals import post_save
from django.test import Client
from jsonschema import Draft202012Validator

from tracepanel.core.context import start_trace
from tracepanel.core.model import Trace


def by_kind(trace: Trace, *kinds: str) -> list[tuple[str, str, str | None]]:
    """(kind, name, parent kind) of the spans of the given kinds, in order."""
    spans = {span.id: span for span in trace.spans}
    return [
        (span.kind, span.name, spans[span.parent_id].kind if span.parent_id in spans else None)
        for span in trace.spans
        if span.kind in kinds
    ]


@pytest.mark.django_db
def test_signal_and_receiver_spans(client: Client, traces: list[Trace], validator: Draft202012Validator) -> None:
    client.get("/create-group/")
    (trace,) = traces
    signal = next(span for span in trace.spans if span.kind == "signal")

    assert signal.name == "signal post_save"
    assert signal.attrs == {
        "signal.name": "django.db.models.signals.post_save",
        "signal.sender": "django.contrib.auth.models.Group",
        "signal.receivers": ["tests.project.views.group_saved"],
    }
    assert signal.duration == IsPositiveInt
    assert by_kind(trace, "signal", "function") == [
        ("signal", "signal post_save", "view"),
        ("function", "tests.project.views.group_saved", "signal"),
    ]
    assert [name for kind, name, parent in by_kind(trace, "sql") if parent == "function"] == ["SELECT COUNT auth_user"]
    validator.validate(trace.to_dict())


@pytest.mark.django_db
def test_callers_get_their_receivers_back() -> None:
    group = Group.objects.create(name="admins")

    with start_trace("command", "test"):
        responses = post_save.send(sender=Group, instance=group, created=False)

    assert [receiver.__name__ for receiver, _ in responses] == ["group_saved"]


@pytest.mark.django_db
def test_model_init_signals_are_left_out(client: Client, traces: list[Trace]) -> None:
    client.get("/n-plus-one/")
    (trace,) = traces

    assert by_kind(trace, "signal") == []
