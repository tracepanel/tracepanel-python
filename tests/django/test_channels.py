import asyncio
import contextlib
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

pytest.importorskip("channels")

from asgiref.sync import async_to_sync
from channels.layers import BaseChannelLayer, get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from channels.worker import Worker
from dirty_equals import IsInstance, IsInt, IsList, IsPartialDict, IsPositiveInt, IsStr
from django.contrib.auth.models import User
from jsonschema import Draft202012Validator

from tests.agentstore import AgentStore, issues
from tests.project import sockets
from tests.tasklive import Recorder, heartbeats, live, live_when, named, setenv, stopped, without_worker_events
from tracepanel._compat import override
from tracepanel.core.context import start_trace
from tracepanel.core.model import Link, Trace
from tracepanel.django import channels as integration
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import set_writer

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
    assert [issue.kind for issue in issues(failed)] == ["error"]


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
def test_consumers_page(traces: list[Trace], store: AgentStore, users: list[User]) -> None:
    store.max_traces = 100
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


# ---------- worker channels: messages runworker handles are tasks ----------

# The worker a test's Worker speaks for: this process.
WORKER = f"channels@{socket.gethostname()}:{os.getpid()}"
GENERATE = "tests.project.sockets.ThumbnailConsumer.thumbnails_generate"
BUILD = "tests.project.sockets.ReportConsumer.report_build"
# Channels' Worker hands a sync consumer a partial to send with, which asgiref warns of: not tracepanel's.
SYNC_WORKER = pytest.mark.filterwarnings("ignore:async_to_sync was passed a non-async-marked callable")


def send(channel: str, message: dict[str, object]) -> None:
    async_to_sync(get_channel_layer().send)(channel, message)  # type: ignore[union-attr]  # the test layer is set


async def serve(done: Callable[[], bool]) -> None:
    """What `runworker thumbnails reports` does, until `done`; the layer is emptied for the next test."""
    layer = get_channel_layer()
    assert layer is not None
    worker = Worker(application=sockets.application, channels=["thumbnails", "reports"], channel_layer=layer)
    listening = asyncio.ensure_future(worker.handle())
    deadline = time.monotonic() + 10
    try:
        while not done() and time.monotonic() < deadline:  # noqa: ASYNC110 (what the worker recorded, polled)
            await asyncio.sleep(0.01)
    finally:
        futures = [listening, *(details["future"] for details in worker.application_instances.values())]
        for future in futures:
            future.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(*futures, return_exceptions=True)
        await layer.flush()  # type: ignore[attr-defined]  # the in-memory layer's extension


def work(done: Callable[[], bool]) -> None:
    async_to_sync(serve)(done)


def tasks(traces: list[Trace]) -> list[Trace]:
    return [trace for trace in traces if trace.kind == "task"]


def own(trace: Trace) -> dict[str, object]:
    """A run's attributes of its own: without the process's (service.*, process.*, …)."""
    return {key: value for key, value in trace.attrs.items() if key.startswith(("task.", "messaging."))}


@SYNC_WORKER
@pytest.mark.django_db(transaction=True)
def test_a_message_to_a_worker_channel_is_a_task_linked_to_its_send(
    traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    with start_trace("http", "POST /images/") as request:
        send("thumbnails", {"type": "thumbnails.generate", "image_id": 7})
    work(lambda: len(tasks(traces)) == 2)
    generate, build = sorted(tasks(traces), key=lambda trace: trace.name != GENERATE)
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]
    (follow,) = [span for span in generate.spans if span.kind == "task.enqueue"]

    assert (enqueue.name, enqueue.attrs) == (
        f"enqueue {GENERATE}",
        {
            "task.system": "channels",
            "task.name": GENERATE,
            "task.id": generate.attrs["task.id"],
            "task.queue": "thumbnails",
            "messaging.system": "memory",
        },
    )
    assert follow.attrs == {
        "task.system": "channels",
        "task.name": BUILD,
        "task.id": build.attrs["task.id"],
        "task.queue": "reports",
        "messaging.system": "memory",
    }
    assert (generate.name, own(generate)) == (
        GENERATE,
        {
            "task.system": "channels",
            "task.name": GENERATE,
            "task.id": IsStr(min_length=1),
            "task.queue": "thumbnails",
            "task.worker": WORKER,
            "task.retries": 0,
            "task.eager": False,
            "task.trigger": "request",
            "task.queue_wait": IsInt(ge=0),
            "messaging.system": "memory",
            "task.args": "()",
            "task.kwargs": "{'image_id': 7}",
            "task.state": "SUCCESS",
        },
    )
    assert generate.links == [Link(kind="enqueued_by", trace_id=request.id, span_id=enqueue.id)]
    assert [(span.kind, span.name) for span in generate.spans[1:]] == [
        ("sql", "SELECT COUNT auth_user"),
        ("task.enqueue", f"enqueue {BUILD}"),
    ]

    # The message a run sends follows it; a sync consumer's handler runs in a thread, with the ORM.
    assert build.attrs == IsPartialDict(
        {
            "task.name": BUILD,
            "task.queue": "reports",
            "task.trigger": "task",
            "task.parent_id": generate.attrs["task.id"],
            "task.root_id": generate.attrs["task.id"],
            "task.kwargs": "{'members': 5}",
        }
    )
    assert build.links == [Link(kind="enqueued_by", trace_id=generate.id, span_id=follow.id)]
    assert [(span.kind, span.name) for span in build.spans[1:]] == [("sql", "SELECT COUNT auth_user")]

    for trace in (request, generate, build):
        validator.validate(trace.to_dict())


@pytest.mark.django_db(transaction=True)
def test_a_handler_that_raises_fails_its_run(traces: list[Trace]) -> None:
    send("thumbnails", {"type": "thumbnails.generate", "image_id": 0})
    work(lambda: bool(tasks(traces)))
    (failed,) = tasks(traces)

    assert (failed.status, failed.attrs["task.state"], failed.attrs["task.trigger"]) == ("error", "FAILURE", "other")
    assert [(error.type, error.message, error.handled) for error in failed.errors] == [
        ("builtins.ValueError", "no such image", False)
    ]
    assert [issue.kind for issue in issues(failed)] == ["error"]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


class Waiting(BaseChannelLayer):
    """A channel layer with no message for the worker: it waits until it is stopped."""

    @override
    async def receive(self, channel: str) -> dict[str, object]:
        await asyncio.Event().wait()
        raise AssertionError(channel)


def test_a_worker_stopped_by_ctrl_c_says_so(recorder: Recorder) -> None:
    def interrupt() -> None:
        # Ctrl+C, once the worker said it is there.
        deadline = time.monotonic() + 10
        while not heartbeats(recorder.events, WORKER) and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    # Python's own handler, whatever an earlier test's worker set (db_worker's): asyncio cancels its run on it.
    handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    threading.Thread(target=interrupt, daemon=True).start()
    try:
        # What `runworker thumbnails` does.
        Worker(application=sockets.application, channels=["thumbnails"], channel_layer=Waiting()).run()
    finally:
        signal.signal(signal.SIGINT, handler)

    # It beats as it starts (and again if the interrupt is slow to come, on a busy machine), then says it stopped, last.
    said = [event["event"] for event in recorder.events if event.get("worker") == WORKER]
    assert said[0] == "heartbeat"
    assert said[-1] == "worker_stopped"
    assert set(said[:-1]) == {"heartbeat"}
    assert stopped(recorder.events, WORKER) == [
        {"event": "worker_stopped", "system": "channels", "id": "", "time": IsPositiveInt, "worker": WORKER}
    ]


def finished(recorder: Recorder, count: int = 1) -> Callable[[], bool]:
    return lambda: len([event for event in recorder.events if event["event"] == "finished"]) >= count


@pytest.mark.django_db(transaction=True)
def test_a_sent_message_tells_the_agent_each_step(recorder: Recorder) -> None:
    with start_trace("http", "POST /images/") as request:
        send("thumbnails", {"type": "thumbnails.wait", "image_id": 7})
    work(finished(recorder))
    (enqueue,) = [span for span in request.spans if span.kind == "task.enqueue"]
    task_id = enqueue.attrs["task.id"]

    assert without_worker_events(recorder.events) == [
        {
            "event": "enqueued",
            "system": "channels",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.project.sockets.ThumbnailConsumer.thumbnails_wait",
            "queue": "thumbnails",
            "retries": 0,
            "args": "()",
            "kwargs": "{'image_id': 7}",
            "trace_id": request.id,
            "span_id": enqueue.id,
        },
        {
            "event": "started",
            "system": "channels",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "tests.project.sockets.ThumbnailConsumer.thumbnails_wait",
            "queue": "thumbnails",
            "worker": WORKER,
            "retries": 0,
            "args": "()",
            "kwargs": "{'image_id': 7}",
        },
        {
            "event": "finished",
            "system": "channels",
            "id": task_id,
            "time": IsPositiveInt,
            "worker": WORKER,
            "state": "SUCCESS",
        },
    ]

    # The worker says it is there as it starts, before it handles anything.
    assert heartbeats(recorder.events, WORKER)[0] == {
        "event": "heartbeat",
        "system": "channels",
        "id": "",
        "time": IsPositiveInt,
        "worker": WORKER,
        "ids": [],
    }


@pytest.mark.django_db(transaction=True)
def test_a_message_sent_outside_a_trace_is_enqueued_without_a_span(recorder: Recorder) -> None:
    send("thumbnails", {"type": "thumbnails.wait"})
    work(finished(recorder))
    enqueued, started, done = without_worker_events(recorder.events)

    assert enqueued == {
        "event": "enqueued",
        "system": "channels",
        "id": started["id"],
        "time": IsPositiveInt,
        "name": "tests.project.sockets.ThumbnailConsumer.thumbnails_wait",
        "queue": "thumbnails",
        "retries": 0,
        "args": "()",
        "kwargs": "{}",
    }
    assert (done["id"], done["state"]) == (started["id"], "SUCCESS")


@pytest.mark.django_db(transaction=True)
def test_websocket_events_and_group_sends_are_not_tasks(recorder: Recorder) -> None:
    async_to_sync(chat)({"text": "hi"})

    assert without_worker_events(recorder.events) == []
    assert [trace.kind for trace in recorder.traces] == ["websocket"] * 7


@SYNC_WORKER
@pytest.mark.django_db(transaction=True)
def test_ignored_messages_send_no_events(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.thumbnails_wait")
    # One consumer handles a channel's messages in order: the second one ends after the first.
    send("thumbnails", {"type": "thumbnails.wait"})
    send("thumbnails", {"type": "thumbnails.generate", "image_id": 7})
    work(finished(recorder))

    assert {event["name"] for event in without_worker_events(recorder.events) if "name" in event} == {GENERATE, BUILD}
    assert {trace.name for trace in recorder.traces} <= {GENERATE, BUILD}


@pytest.mark.django_db(transaction=True)
def test_arguments_are_left_out_of_the_events_too(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")
    send("thumbnails", {"type": "thumbnails.wait", "secret": "x"})
    work(finished(recorder))

    assert [{"args", "kwargs"} & event.keys() for event in without_worker_events(recorder.events)] == [set()] * 3


def test_channels_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


@pytest.mark.django_db(transaction=True)
def test_the_agent_lists_a_message_queued_then_running_until_it_ends(store: AgentStore) -> None:
    live(store)
    set_writer(store.writer())
    go = threading.Event()
    sockets.release.clear()

    async def flow() -> None:
        # One loop for the sender and the worker: the in-memory layer's queues belong to it.
        await get_channel_layer().send("thumbnails", {"type": "thumbnails.wait", "image_id": 7})  # type: ignore[union-attr]
        await asyncio.to_thread(go.wait, 10)
        await serve(lambda: sockets.release.is_set() and not integration._handling_ids())  # noqa: SLF001

    thread = threading.Thread(target=async_to_sync(flow))
    thread.start()
    try:
        queued = live_when(store, lambda answer: bool(answer["tasks"]))

        assert queued == {"now": IsPositiveInt, "tasks": [IsPartialDict(state="queued")], "workers": IsInstance(list)}
        assert queued["tasks"][0] == {
            "id": IsStr(min_length=1),
            "system": "channels",
            "name": "tests.project.sockets.ThumbnailConsumer.thumbnails_wait",
            "service": IsStr,
            "state": "queued",
            "queue": "thumbnails",
            "worker": None,
            "enqueued_at": IsPositiveInt,
            "started_at": None,
            "retries": 0,
            "args": "()",
            "kwargs": "{'image_id': 7}",
            "error": None,
            "enqueued_by": None,
        }

        go.set()
        running = live_when(store, lambda answer: answer["tasks"][0]["state"] == "running")

        assert running["tasks"] == [IsPartialDict(state="running", worker=WORKER, started_at=IsInt)]
        assert named(running, WORKER) == [
            {"name": WORKER, "system": "channels", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        sockets.release.set()
        done = live_when(store, lambda answer: not answer["tasks"])

        assert named(done, WORKER) == [IsPartialDict(name=WORKER, in_flight=0)]
    finally:
        go.set()
        sockets.release.set()
        thread.join(15)
        set_writer(None)


def test_a_redis_layer_names_its_server() -> None:
    def redis(*hosts: dict[str, object]) -> object:
        # How channels_redis keeps CONFIG's `hosts`: a ("host", port) pair as host and port, a URL as address.
        return type("RedisChannelLayer", (), {"hosts": list(hosts)})()

    assert integration._layer_attrs(redis({"host": "127.0.0.1", "port": 63811})) == {  # noqa: SLF001
        "server.address": "127.0.0.1",
        "server.port": 63811,
        "messaging.system": "redis",
    }
    assert integration._layer_attrs(redis({"address": "redis://cache.internal:6380"})) == {  # noqa: SLF001
        "server.address": "cache.internal",
        "server.port": 6380,
        "messaging.system": "redis",
    }


@pytest.fixture
def redis_layer(settings: Any) -> Iterator[None]:  # noqa: ANN401 (pytest-django's settings)
    """CHANNEL_LAYERS on channels-redis, its Redis a fakeredis server in this process."""
    pytest.importorskip("channels_redis")
    # fakeredis runs the Lua scripts channels-redis's receive sends with lupa.
    pytest.importorskip("lupa")
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hosts = [f"redis://127.0.0.1:{server.server_address[1]}"]
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": hosts}}
    }
    yield
    server.shutdown()
    server.server_close()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("redis_layer")
def test_a_redis_layer_s_own_commands_are_not_redis_spans(traces: list[Trace], users: list[User]) -> None:
    async def post() -> None:
        client = WebsocketCommunicator(APP, "/ws/chat/lobby/")
        await client.connect()
        await client.send_json_to({"text": "hi"})
        replies = [await client.receive_json_from(5), await client.receive_json_from(5)]
        assert replies == [{"members": len(users)}, {"text": "hi"}]
        await client.disconnect()

    async_to_sync(post)()
    receive = next(trace for trace in traces if trace.name == "WS receive /ws/chat/lobby/")

    assert [(span.kind, span.name) for span in receive.spans[1:]] == [
        ("sql", "SELECT COUNT auth_user"),
        ("channel.send", "group_send lobby"),
        ("websocket.send", "send"),
    ]

    # Its sorted sets, their expiry and capacity checks, and each receive's blocking pop are the transport's.
    assert [span.name for trace in traces for span in trace.spans if span.kind == "redis"] == []


@pytest.mark.django_db(transaction=True)
def test_a_message_a_profiled_request_sends_to_a_worker_is_profiled_too(traces: list[Trace]) -> None:
    with start_trace("http", "POST /images/", profile=True):
        send("thumbnails", {"type": "thumbnails.crunch", "label": "asked"})
    with start_trace("http", "GET /images/"):
        send("thumbnails", {"type": "thumbnails.crunch", "label": "plain"})
    work(lambda: len(tasks(traces)) == 2)

    assert {str(trace.attrs["task.kwargs"]): trace.profile is not None for trace in tasks(traces)} == {
        "{'label': 'asked'}": True,
        "{'label': 'plain'}": False,
    }
