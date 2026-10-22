import sys
import threading
import types
from collections.abc import Generator
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from dirty_equals import IsInstance, IsPositiveInt, IsStr
from django.contrib.auth.models import Group, User
from django.core.signals import request_finished, request_started
from django.db.models.signals import post_init, post_save, pre_init
from django.dispatch import Signal
from django.test import AsyncClient, Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tests.project import receivers, views
from tests.project.signals import cart_viewed, stock_changed
from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.django import signals

ID = IsStr(regex=r"[0-9A-Z]{26}")


def by_kind(trace: Trace, *kinds: str) -> list[tuple[str, str, str | None]]:
    """(kind, name, parent name) of the spans of the given kinds, in order."""
    spans = {span.id: span for span in trace.spans}
    return [
        (span.kind, span.name, spans[span.parent_id].name if span.parent_id in spans else None)
        for span in trace.spans
        if span.kind in kinds
    ]


def of_kind(trace: Trace, kind: str) -> list[Span]:
    return [span for span in trace.spans if span.kind == kind]


def line(function: object) -> int:
    return function.__code__.co_firstlineno  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_signal_and_receiver_spans(client: Client, traces: list[Trace], validator: Draft202012Validator) -> None:
    client.get("/create-group/")
    (trace,) = traces
    (signal,) = of_kind(trace, "signal")
    (receiver,) = [span for span in trace.spans if span.parent_id == signal.id]

    assert signal.to_dict() == {
        "id": ID,
        "parent_id": trace.spans[1].id,
        "kind": "signal",
        "name": "post_save",
        "start": IsPositiveInt,
        "duration": IsPositiveInt,
        "status": "ok",
        "attrs": {
            "signal.name": "django.db.models.signals.post_save",
            "signal.sender": "auth.Group",
            "signal.receivers": ["tests.project.views.group_saved"],
            "code.origin": IsStr(regex=r"tests/project/views\.py:\d+ in create_group"),
        },
    }
    assert receiver.to_dict() == {
        "id": ID,
        "parent_id": signal.id,
        "kind": "function",
        "name": "tests.project.views.group_saved",
        "start": IsPositiveInt,
        "duration": IsPositiveInt,
        "status": "ok",
        "attrs": {
            "code.function": "tests.project.views.group_saved",
            "code.filepath": "tests/project/views.py",
            "code.lineno": line(views.group_saved),
            "signal.name": "django.db.models.signals.post_save",
        },
    }

    assert by_kind(trace, "sql") == [
        ("sql", "INSERT auth_group", "tests.project.views.create_group"),
        ("sql", "SELECT COUNT auth_user", "tests.project.views.group_saved"),
    ]

    # pre_save has no receiver: counted, no span.
    assert trace.attrs[signals.UNRECEIVED] == ["pre_save auth.Group: 1"]
    validator.validate(trace.to_dict())


@pytest.mark.django_db
def test_callers_get_their_receivers_back() -> None:
    group = Group.objects.create(name="admins")

    with start_trace("command", "test"):
        responses = post_save.send(sender=Group, instance=group, created=False)

    assert responses == [(views.group_saved, None)]


@pytest.mark.django_db
def test_delete_and_m2m_changed() -> None:
    group = Group.objects.create(name="admins")
    user = User.objects.create_user("ann")

    with start_trace("command", "test") as trace:
        user.groups.add(group)
        group.delete()

    # pre_add and post_add, then post_delete, each inside the ORM's savepoint. How many savepoints the ORM nests
    # differs by Django version (from 6.0, one fewer around the INSERT): transaction spans are left out of the order.
    assert by_kind(trace, "signal", "function", "sql") == [
        ("sql", "SELECT auth_user_groups", "savepoint default"),
        ("signal", "m2m_changed", "savepoint default"),
        ("function", "tests.project.receivers.membership_changed", "m2m_changed"),
        ("sql", "INSERT auth_user_groups", "savepoint default"),
        ("signal", "m2m_changed", "savepoint default"),
        ("function", "tests.project.receivers.membership_changed", "m2m_changed"),
        ("sql", "SELECT COUNT auth_group", "tests.project.receivers.membership_changed"),
        ("sql", "DELETE auth_group_permissions", "savepoint default"),
        ("sql", "DELETE auth_user_groups", "savepoint default"),
        ("sql", "DELETE auth_group", "savepoint default"),
        ("signal", "post_delete", "savepoint default"),
        ("function", "tests.project.receivers.group_deleted", "post_delete"),
        ("sql", "SELECT COUNT auth_user", "tests.project.receivers.group_deleted"),
    ]

    assert [span.attrs for span in of_kind(trace, "signal")] == [
        {
            "signal.name": "django.db.models.signals.m2m_changed",
            "signal.sender": "auth.User_groups",
            "signal.receivers": ["tests.project.receivers.membership_changed"],
            "code.origin": IsStr(regex=r"tests/django/test_signals\.py:\d+ in test_delete_and_m2m_changed"),
        },
        {
            "signal.name": "django.db.models.signals.m2m_changed",
            "signal.sender": "auth.User_groups",
            "signal.receivers": ["tests.project.receivers.membership_changed"],
            "code.origin": IsStr(regex=r"tests/django/test_signals\.py:\d+ in test_delete_and_m2m_changed"),
        },
        {
            "signal.name": "django.db.models.signals.post_delete",
            "signal.sender": "auth.Group",
            "signal.receivers": ["tests.project.receivers.group_deleted"],
            "code.origin": IsStr(regex=r"tests/django/test_signals\.py:\d+ in test_delete_and_m2m_changed"),
        },
    ]

    assert trace.attrs[signals.UNRECEIVED] == ["pre_delete auth.Group: 1"]


@pytest.mark.django_db
def test_user_logged_in_runs_django_s_own_receiver(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/log-in/")
    (trace,) = traces
    (signal,) = of_kind(trace, "signal")
    receiver = next(span for span in trace.spans if span.parent_id == signal.id)

    assert (signal.name, signal.attrs) == (
        "user_logged_in",
        {
            "signal.name": "django.contrib.auth.signals.user_logged_in",
            "signal.sender": "auth.User",
            "signal.receivers": ["django.contrib.auth.models.update_last_login"],
            # login() sent it, called from the view.
            "code.origin": IsStr(regex=r"tests/project/views\.py:\d+ in log_in"),
        },
    )
    assert receiver.attrs == {
        "code.function": "django.contrib.auth.models.update_last_login",
        "code.filepath": "django/contrib/auth/models.py",
        "code.lineno": IsPositiveInt,
        "signal.name": "django.contrib.auth.signals.user_logged_in",
    }

    assert [name for kind, name, parent in by_kind(trace, "sql") if parent == receiver.name] == ["UPDATE auth_user"]


@pytest.mark.django_db
def test_a_project_signal_is_named_by_its_module_and_send_robust_keeps_the_error() -> None:
    with start_trace("command", "test") as trace:
        responses = stock_changed.send_robust(sender=Group, sku="A-1")
    (signal,) = of_kind(trace, "signal")
    reserve, notify = [span for span in trace.spans if span.parent_id == signal.id]

    assert responses == [
        (receivers.reserve_stock, "reserved 0"),
        (receivers.notify_warehouse, IsInstance(ConnectionError)),
    ]

    assert (signal.name, signal.status, signal.attrs) == (
        "tests.project.signals.stock_changed",
        "ok",
        {
            "signal.name": "tests.project.signals.stock_changed",
            "signal.sender": "auth.Group",
            "signal.receivers": ["tests.project.receivers.reserve_stock", "tests.project.receivers.notify_warehouse"],
            "signal.robust": True,
            "code.origin": IsStr(regex=r"tests/django/test_signals\.py:\d+ in test_a_project_signal_is_named_by_.*"),
        },
    )
    assert (reserve.name, reserve.status) == ("tests.project.receivers.reserve_stock", "ok")
    assert (notify.name, notify.status, notify.attrs) == (
        "tests.project.receivers.notify_warehouse",
        "error",
        {
            "code.function": "tests.project.receivers.notify_warehouse",
            "code.filepath": "tests/project/receivers.py",
            "code.lineno": line(receivers.notify_warehouse),
            "signal.name": "tests.project.signals.stock_changed",
            "exception.type": "builtins.ConnectionError",
            "exception.message": "warehouse offline",
        },
    )

    # The trace is fine: send_robust() handled the error, and Django logged it.
    assert (trace.status, trace.errors) == ("ok", [])
    assert [(log.level, log.span_id, log.message) for log in trace.logs] == [
        ("error", signal.id, IsStr(regex=r"Error calling .*notify_warehouse in Signal\.send_robust\(\) .*")),
    ]


@pytest.mark.django_db
def test_send_raises_what_a_receiver_raised() -> None:
    with pytest.raises(ConnectionError), start_trace("command", "test") as trace:
        stock_changed.send(sender=None)
    (signal,) = of_kind(trace, "signal")

    assert [(span.name, span.status) for span in trace.spans if span.parent_id == signal.id] == [
        ("tests.project.receivers.reserve_stock", "ok"),
        ("tests.project.receivers.notify_warehouse", "error"),
    ]
    assert (signal.status, trace.status) == ("error", "error")
    assert "signal.sender" not in signal.attrs


# A library's module, as installed: no project code on the stack when it sends.
LIBRARY = """
from tracepanel.core.context import start_trace

def fire(signal, out):
    with start_trace("command", "library") as trace:
        signal.send(sender=None)
    out.append(trace)
"""


def test_a_send_no_project_code_made_names_the_library_that_did() -> None:
    library = types.ModuleType("somelib.hooks")
    exec(compile(LIBRARY, "/venv/lib/python3.12/site-packages/somelib/hooks.py", "exec"), vars(library))  # noqa: S102
    tick = Signal()
    tick.connect(views.count_orders, weak=False)
    out: list[Trace] = []
    thread = threading.Thread(target=library.fire, args=(tick, out))  # type: ignore[attr-defined]

    with patch.object(views.User.objects, "count"):
        thread.start()
        thread.join()
    (trace,) = out
    (signal,) = of_kind(trace, "signal")

    # A signal made inside a function: no module holds it, so it has no dotted path.
    assert (signal.name, signal.attrs) == (
        "signal",
        {
            "signal.name": "signal",
            "signal.receivers": ["tests.project.views.count_orders"],
            "code.origin": None,
            "code.caller": "somelib/hooks.py:6 in fire",
        },
    )


@pytest.mark.django_db
def test_signals_nobody_receives_are_counted() -> None:
    with start_trace("command", "test") as trace:
        for _ in range(3):
            cart_viewed.send(sender=Group)
        cart_viewed.send(sender=None)
        # An instance, unhashable while unsaved: by its class.
        cart_viewed.send(sender=Group(name="new"))

    assert of_kind(trace, "signal") == []

    assert trace.attrs == {
        signals.UNRECEIVED: ["tests.project.signals.cart_viewed auth.Group: 4", "tests.project.signals.cart_viewed: 1"]
    }


@pytest.mark.django_db
def test_model_init_signals_are_left_out(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/n-plus-one/")
    (trace,) = traces

    assert by_kind(trace, "signal") == []
    assert signals.UNRECEIVED not in trace.attrs

    # Not even wrapped: Django's own send, for every object a query loads.
    assert (pre_init.send.__func__, post_init.send.__func__) == (signals.original(Signal, "send"),) * 2


def test_receivers_of_ignored_signals_still_run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.TRACEPANEL = {"ignore_signals": ["tests.noisy.*"]}
    module = types.ModuleType("tests.noisy")
    module.tick = Signal()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tests.noisy", module)
    ticks: list[object] = []
    module.tick.connect(lambda sender, **_: ticks.append(sender), weak=False)  # type: ignore[attr-defined]

    with start_trace("command", "test") as trace:
        module.tick.send(sender=1)  # type: ignore[attr-defined]
        module.tick.send(sender=2)  # type: ignore[attr-defined]

    assert (ticks, trace.spans[1:], trace.attrs) == ([1, 2], [], {})


@pytest.fixture
def started_receiver() -> Generator[list[str], None, None]:
    calls: list[str] = []

    def opened(sender: object, **kwargs: object) -> None:
        calls.append("started")
        Group.objects.count()

    def closed(sender: object, **kwargs: object) -> None:
        calls.append("finished")

    request_started.connect(opened, dispatch_uid="tests.opened")
    request_finished.connect(closed, dispatch_uid="tests.closed")
    yield calls
    request_started.disconnect(dispatch_uid="tests.opened")
    request_finished.disconnect(dispatch_uid="tests.closed")


@pytest.mark.django_db
def test_request_started_opens_the_request_s_trace(
    client: Client, traces: list[Trace], started_receiver: list[str]
) -> None:
    client.get("/")
    (trace,) = traces
    root, signal = trace.spans[:2]
    (view,) = of_kind(trace, "view")

    assert started_receiver == ["started", "finished"]

    assert by_kind(trace, "signal", "function", "sql", "view") == [
        ("signal", "request_started", "GET /"),
        # With a receiver of the app's, Django's own show too.
        ("function", "django.db.reset_queries", "request_started"),
        ("function", "tests.django.test_signals.started_receiver.<locals>.opened", "request_started"),
        ("sql", "SELECT COUNT auth_group", "tests.django.test_signals.started_receiver.<locals>.opened"),
        ("view", "tests.project.views.home", "GET /"),
    ]

    assert signal.attrs == {
        "signal.name": "django.core.signals.request_started",
        "signal.sender": "django.test.client.ClientHandler",
        "signal.receivers": ["django.db.reset_queries", "tests.django.test_signals.started_receiver.<locals>.opened"],
        # The test client was called from here. Under a server no project code sends it: code.caller names the server.
        "code.origin": IsStr(
            regex=r"tests/django/test_signals\.py:\d+ in test_request_started_opens_the_request_s_trace"
        ),
    }

    # The trace starts with the signal, and lasts until the response.
    assert (trace.start, trace.start <= signal.start) == (root.start, True)
    assert (trace.duration, root.start + root.duration >= view.start + view.duration) == (root.duration, True)


@pytest.mark.django_db
def test_request_started_with_only_django_s_receivers_is_left_out(client: Client, traces: list[Trace]) -> None:
    client.get("/")
    client.get("/")

    assert [by_kind(trace, "signal") for trace in traces] == [[], []]


@pytest.mark.django_db
def test_async_receivers_under_asgi(traces: list[Trace], users: list[User]) -> None:
    client = AsyncClient()

    async_to_sync(client.get)("/async/users/")
    (trace,) = traces
    (signal,) = of_kind(trace, "signal")
    receivers_ = sorted(
        ((span.name, span.attrs["code.lineno"]) for span in trace.spans if span.parent_id == signal.id),
    )

    assert (signal.name, signal.attrs) == (
        "tests.project.views.order_placed",
        {
            "signal.name": "tests.project.views.order_placed",
            "signal.receivers": ["tests.project.views.count_orders", "tests.project.views.notify_shop"],
            "code.origin": IsStr(regex=r"tests/project/views\.py:\d+ in async_users"),
        },
    )
    assert receivers_ == [
        ("tests.project.views.count_orders", line(views.count_orders)),
        ("tests.project.views.notify_shop", line(views.notify_shop)),
    ]


@pytest.mark.django_db
def test_asend_robust_keeps_an_async_receiver_s_error() -> None:
    calls: list[str] = []

    async def fails(sender: object, **kwargs: object) -> None:
        calls.append("fails")
        message = "async receiver failed"
        raise ValueError(message)

    async def send() -> Trace:
        with start_trace("command", "test") as trace:
            await stock_changed.asend_robust(sender=None)
        return trace

    stock_changed.connect(fails, dispatch_uid="tests.fails")
    try:
        trace = async_to_sync(send)()
    finally:
        stock_changed.disconnect(dispatch_uid="tests.fails")
    (signal,) = of_kind(trace, "signal")

    assert calls == ["fails"]

    assert sorted(
        (span.name, span.status, span.attrs.get("exception.type"))
        for span in trace.spans
        if span.parent_id == signal.id
    ) == [
        (
            "tests.django.test_signals.test_asend_robust_keeps_an_async_receiver_s_error.<locals>.fails",
            "error",
            "builtins.ValueError",
        ),
        ("tests.project.receivers.notify_warehouse", "error", "builtins.ConnectionError"),
        ("tests.project.receivers.reserve_stock", "ok", None),
    ]
    assert trace.status == "ok"
