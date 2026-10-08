from pathlib import Path

import pytest

pytest.importorskip("channels")

from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from dirty_equals import IsInt, IsList, IsPartialDict
from django.contrib.auth.models import User
from jsonschema import Draft202012Validator

from tests.project import sockets
from tracepanel.core.model import Trace
from tracepanel.storage.store import Store

APP = URLRouter(sockets.urls)


def events(traces: list[Trace]) -> list[str]:
    return [trace.name for trace in traces]


async def chat(*messages: dict[str, object]) -> list[dict[str, object]]:
    """Two clients in one room: the first one posts, both receive."""
    first = WebsocketCommunicator(APP, "/ws/chat/lobby/")
    second = WebsocketCommunicator(APP, "/ws/chat/lobby/")
    await first.connect()
    await second.connect()
    replies: list[dict[str, object]] = []
    for message in messages:
        await first.send_json_to(message)
        replies += [await first.receive_json_from(), await first.receive_json_from(), await second.receive_json_from()]
    await first.disconnect()
    await second.disconnect()
    return replies


@pytest.mark.django_db(transaction=True)
def test_websocket_events_are_traces(traces: list[Trace], users: list[User], validator: Draft202012Validator) -> None:
    async_to_sync(chat)({"text": "hi"})
    by_name = {trace.name: trace for trace in traces}
    connect = traces[0]
    receive = by_name["WS receive /ws/chat/lobby/"]
    send_span = next(span for span in receive.spans if span.kind == "channel.send")
    delivered = [trace for trace in traces if trace.name == "WS chat.message /ws/chat/lobby/"]

    assert sorted(events(traces)) == [
        "WS chat.message /ws/chat/lobby/",
        "WS chat.message /ws/chat/lobby/",
        "WS connect /ws/chat/lobby/",
        "WS connect /ws/chat/lobby/",
        "WS disconnect /ws/chat/lobby/",
        "WS disconnect /ws/chat/lobby/",
        "WS receive /ws/chat/lobby/",
    ]
    assert connect.attrs == IsPartialDict(
        {
            "ws.event": "connect",
            "ws.path": "/ws/chat/lobby/",
            "code.view": "tests.project.sockets.ChatConsumer",
            "ws.accepted": True,
        }
    )
    assert receive.attrs == IsPartialDict({"ws.event": "receive", "ws.message_size": 14})
    assert [(span.kind, span.name) for span in receive.spans[1:]] == [
        ("sql", "SELECT COUNT auth_user"),
        ("channel.send", "group_send lobby"),
        ("websocket.send", "send"),
    ]
    assert send_span.attrs == {
        "messaging.system": "memory",
        "channel.group": "lobby",
        "channel.message_type": "chat.message",
    }
    # Each delivery links to its connection and to the span that sent the group message.
    assert [link.kind for trace in delivered for link in trace.links] == ["connection", "sent_by"] * 2
    assert {link.span_id for trace in delivered for link in trace.links if link.kind == "sent_by"} == {send_span.id}
    assert [(link.kind, link.trace_id) for link in receive.links] == [("connection", connect.id)]
    assert all(trace.status == "ok" for trace in traces)
    for trace in traces:
        validator.validate(trace.to_dict())


@pytest.mark.django_db(transaction=True)
def test_handler_error_is_recorded(traces: list[Trace]) -> None:
    async def post_boom() -> None:
        client = WebsocketCommunicator(APP, "/ws/chat/lobby/")
        await client.connect()
        await client.send_json_to({"text": "boom"})
        with pytest.raises(ValueError, match="cannot post"):
            await client.wait()

    async_to_sync(post_boom)()
    (failed,) = [trace for trace in traces if trace.status == "error"]

    assert failed.name == "WS receive /ws/chat/lobby/"
    assert [(error.type, error.message) for error in failed.errors] == [
        ("builtins.ValueError", "cannot post to this room")
    ]
    assert [issue.kind for issue in failed.issues] == ["error"]


@pytest.mark.django_db(transaction=True)
def test_sync_consumer(traces: list[Trace], users: list[User]) -> None:
    async def echo() -> str | bytes:
        client = WebsocketCommunicator(APP, "/ws/echo/")
        await client.connect()
        await client.send_to(text_data="ping")
        reply = await client.receive_from()
        await client.disconnect()
        return reply

    assert async_to_sync(echo)() == "ping (5 users)"
    receive = next(trace for trace in traces if trace.attrs["ws.event"] == "receive")

    assert [(span.kind, span.name) for span in receive.spans[1:]] == [
        ("sql", "SELECT COUNT auth_user"),
        ("websocket.send", "send"),
    ]


@pytest.mark.django_db(transaction=True)
def test_consumers_page(traces: list[Trace], tmp_path: Path, users: list[User]) -> None:
    store = Store(tmp_path / "traces.sqlite3")
    async_to_sync(chat)({"text": "hi"}, {"text": "again"})
    store.write(traces)

    assert store.list_websockets() == [
        {
            "consumer": "tests.project.sockets.ChatConsumer",
            "connections": 2,
            "received": 2,
            "delivered": 4,
            "disconnects": 2,
            "errors": 0,
            "p50": IsInt(ge=0),
            "p95": IsInt(ge=0),
            "total": IsInt(ge=0),
            "avg_queries": 0.2,
            "with_issues": 0,
            "paths": ["/ws/chat/lobby/"],
            "last_seen": IsInt,
        }
    ]
    assert [row["received"] for row in store.list_websockets("event:receive")] == [2]

    connect = traces[0]
    receive = next(trace for trace in traces if trace.name == "WS receive /ws/chat/lobby/")
    related = store.related(receive.id)
    assert (related["connection"], len(related["delivered"])) == (IsPartialDict(id=connect.id), 2)
    assert [item["name"] for item in store.related(connect.id)["connection_events"]] == IsList(
        "WS receive /ws/chat/lobby/", check_order=False, length=...
    )
