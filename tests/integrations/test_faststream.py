"""FastStream, on each broker FastStream tests in memory (TestKafkaBroker and the others): no broker server runs.

The in-memory brokers hand a published message to its handler inside the publish, so the handler's trace is made
while the publisher's is open: each still is a trace of its own, linked to the publish.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dirty_equals import IsInt, IsPartialDict, IsPositiveInt, IsStr
from jsonschema import Draft202012Validator

pytest.importorskip("faststream")
pytest.importorskip("aiokafka")

from faststream import AckPolicy
from faststream._internal.broker import BrokerUsecase
from faststream.exceptions import NackMessage, RejectMessage
from faststream.kafka import KafkaBroker, TestKafkaBroker
from faststream.nats import NatsBroker, TestNatsBroker
from faststream.rabbit import RabbitBroker, TestRabbitBroker
from faststream.rabbit.annotations import RabbitMessage
from faststream.redis import RedisBroker, TestRedisBroker

from tests.agentstore import AgentStore
from tests.profiles import busy
from tests.tasklive import Recorder, live, live_when, named, setenv, without_worker_events
from tracepanel.core import conf
from tracepanel.core.context import start_trace
from tracepanel.core.model import Link, Span, Trace
from tracepanel.integrations import faststream as integration
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import set_writer

integration.install()
logger = logging.getLogger("tests.orders")

HANDLER = "tests.integrations.test_faststream.on_order_created"


async def on_order_created(body: dict[str, int]) -> int:
    logger.warning("order %s created", body["id"])
    return body["id"]


async def sync_crm(body: dict[str, int]) -> None:
    customer = body["id"]
    message = f"CRM is down for {customer}"
    raise ConnectionError(message)


async def nack(body: dict[str, int]) -> None:
    raise NackMessage


async def refuse(body: dict[str, int]) -> None:
    raise RejectMessage


async def housekeeping(body: dict[str, int]) -> None:
    pass


def _confluent() -> tuple[Any, Any]:
    # No wheel for every Python FastStream runs on (3.15): the dependency group leaves it out there.
    pytest.importorskip("confluent_kafka")
    confluent = pytest.importorskip("faststream.confluent")
    return confluent.KafkaBroker, confluent.TestKafkaBroker


# name -> (broker, its in-memory twin, the system its messages belong to).
BROKERS: dict[str, Callable[[], tuple[Any, Any]]] = {
    "kafka": lambda: (KafkaBroker, TestKafkaBroker),
    "confluent": _confluent,
    "rabbitmq": lambda: (RabbitBroker, TestRabbitBroker),
    "nats": lambda: (NatsBroker, TestNatsBroker),
    "redis": lambda: (RedisBroker, TestRedisBroker),
}
SYSTEMS = {"kafka": "kafka", "confluent": "kafka", "rabbitmq": "rabbitmq", "nats": "nats", "redis": "redis"}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorded = Recorder()
    monkeypatch.setattr(transport, "_writer", recorded)
    return recorded


def tasks(recorder: Recorder) -> list[Trace]:
    return [trace for trace in recorder.traces if trace.kind == "task"]


def enqueues(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "task.enqueue"]


def handle(
    broker: Any,  # noqa: ANN401 (a FastStream broker)
    testing: Any,  # noqa: ANN401 (its in-memory twin)
    send: Callable[[Any], Awaitable[None]],
) -> None:
    """Run `send(broker)` with the broker in memory, its messages handled as they are published."""

    async def run() -> None:
        async with testing(broker) as tested:
            await send(tested)

    asyncio.run(run())


def test_a_handled_message_is_a_task_trace_linked_to_its_publish(
    recorder: Recorder, validator: Draft202012Validator
) -> None:
    broker = KafkaBroker()
    broker.subscriber("orders")(on_order_created)

    async def send(tested: KafkaBroker) -> None:
        with start_trace("http", "POST /orders") as request:
            await tested.publish({"id": 7}, "orders")
        recorder.traces.append(request)

    handle(broker, TestKafkaBroker, send)
    (task,) = tasks(recorder)
    request = recorder.traces[-1]
    (publish,) = enqueues(request)
    validator.validate(request.to_dict())
    validator.validate(task.to_dict())

    assert publish.to_dict() == {
        "id": IsStr,
        "parent_id": request.spans[0].id,
        "kind": "task.enqueue",
        "name": "publish orders",
        "start": IsPositiveInt,
        "duration": IsInt(ge=0),
        "status": "ok",
        "attrs": {
            "task.system": "kafka",
            "task.name": "orders",
            "task.id": IsStr,
            "task.queue": "orders",
            "messaging.system": "kafka",
            "messaging.operation": "publish",
            "messaging.destination.name": "orders",
            "code.origin": IsStr(regex=r".*tests/integrations/test_faststream\.py:\d+ in send"),
            "w3c.span_id": IsStr(regex=r"^[0-9a-f]{16}$"),
        },
    }

    assert task.to_dict() == {
        "id": IsStr,
        "kind": "task",
        "name": HANDLER,
        "start": IsPositiveInt,
        "duration": IsInt(ge=0),
        "status": "ok",
        "attrs": {
            "task.system": "kafka",
            "task.name": HANDLER,
            "task.id": publish.attrs["task.id"],
            "task.queue": "orders",
            "task.worker": IsStr(regex=r".+@.+"),
            "task.retries": 0,
            "task.eager": False,
            "task.trigger": "message",
            "messaging.system": "kafka",
            "messaging.operation": "process",
            "messaging.destination.name": "orders",
            "messaging.message.id": IsStr,
            "messaging.kafka.partition": 0,
            "messaging.kafka.offset": 0,
            "w3c.trace_id": request.attrs["w3c.trace_id"],
            "task.args": "({'id': 7},)",
            "task.kwargs": "{}",
            "task.state": "SUCCESS",
            "task.result": "7",
            "service.name": IsStr,
            "service.role": "worker",
            "telemetry.sdk.language": "python",
            "process.runtime.name": IsStr,
            "process.runtime.version": IsStr,
            # The test session's framework: Django's settings.
            "tracepanel.framework": IsStr,
        },
        "spans": [
            {
                "id": IsStr,
                "parent_id": None,
                "kind": "task.run",
                "name": HANDLER,
                "start": IsPositiveInt,
                "duration": IsInt(ge=0),
                "status": "ok",
                "attrs": {},
            }
        ],
        "logs": [IsPartialDict(level="warning", logger="tests.orders", message="order 7 created")],
        "errors": [],
        "links": [{"kind": "sent_by", "trace_id": request.id, "span_id": publish.id}],
        "issues": [],
    }


@pytest.mark.parametrize("name", list(BROKERS))
def test_each_broker_says_its_system_and_destination(name: str, recorder: Recorder) -> None:
    broker_class, testing = BROKERS[name]()
    broker = broker_class()
    broker.subscriber("orders")(on_order_created)

    async def send(tested: Any) -> None:  # noqa: ANN401
        with start_trace("http", "POST /orders") as request:
            await tested.publish({"id": 7}, "orders")
        recorder.traces.append(request)

    handle(broker, testing, send)
    (task,) = tasks(recorder)
    (publish,) = enqueues(recorder.traces[-1])

    assert publish.attrs == IsPartialDict(
        {"task.system": SYSTEMS[name], "messaging.system": SYSTEMS[name], "messaging.destination.name": "orders"}
    )

    assert (task.name, task.attrs) == (
        HANDLER,
        IsPartialDict(
            {
                "task.system": SYSTEMS[name],
                "task.id": publish.attrs["task.id"],
                "task.queue": "orders",
                "task.state": "SUCCESS",
                "messaging.destination.name": "orders",
            }
        ),
    )

    assert task.links == [Link(kind="sent_by", trace_id=recorder.traces[-1].id, span_id=publish.id)]


def test_a_publish_carries_the_trace_context_in_its_headers(recorder: Recorder) -> None:
    broker = RabbitBroker()
    seen: list[dict[str, Any]] = []

    @broker.subscriber("orders")
    async def headers_of(body: dict[str, int], message: RabbitMessage) -> None:
        seen.append(dict(message.headers))

    async def send(tested: RabbitBroker) -> None:
        with start_trace("http", "POST /orders") as request:
            await tested.publish({"id": 7}, "orders")
        recorder.traces.append(request)

    handle(broker, TestRabbitBroker, send)
    request = recorder.traces[-1]
    (publish,) = enqueues(request)

    assert seen == [
        IsPartialDict(
            {
                "traceparent": f"00-{request.attrs['w3c.trace_id']}-{publish.attrs['w3c.span_id']}-01",
                "tracepanel-sent-by": f"{request.id}:{publish.id}",
                "tracepanel-task-id": publish.attrs["task.id"],
            }
        )
    ]


def test_a_message_from_an_app_without_tracepanel_follows_its_traceparent(recorder: Recorder) -> None:
    broker = KafkaBroker()
    broker.subscriber("orders")(on_order_created)
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    async def send(tested: KafkaBroker) -> None:
        await tested.publish({"id": 7}, "orders", headers={"traceparent": traceparent})

    handle(broker, TestKafkaBroker, send)
    (task,) = tasks(recorder)

    assert task.attrs == IsPartialDict(
        {
            "task.trigger": "message",
            "w3c.trace_id": "0af7651916cd43dd8448eb211c80319c",
            "w3c.parent_span_id": "b7ad6b7169203331",
        }
    )

    assert task.links == []


def test_a_failure_is_an_error_with_its_locals(recorder: Recorder) -> None:
    broker = KafkaBroker()
    broker.subscriber("crm")(sync_crm)

    async def send(tested: KafkaBroker) -> None:
        # The in-memory broker raises the handler's error where the message was published.
        with pytest.raises(ConnectionError):
            await tested.publish({"id": 7}, "crm")

    handle(broker, TestKafkaBroker, send)
    (task,) = tasks(recorder)
    (error,) = task.errors
    frame = error.frames[-1]

    assert (task.status, task.attrs["task.state"]) == ("error", "FAILURE")

    assert (error.type, error.message, error.handled) == (
        "builtins.ConnectionError",
        "CRM is down for 7",
        False,
    )
    assert (frame.function, frame.locals) == (
        "sync_crm",
        {"body": "{'id': 7}", "customer": "7", "message": "'CRM is down for 7'"},
    )


def test_a_nacked_message_waits_for_its_next_delivery(recorder: Recorder) -> None:
    broker = RabbitBroker()
    broker.subscriber("crm", ack_policy=AckPolicy.NACK_ON_ERROR)(sync_crm)

    async def send(tested: RabbitBroker) -> None:
        # The broker delivers it again: the in-memory one does not, so it is published again with its task id.
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await tested.publish({"id": 7}, "crm", headers={"tracepanel-task-id": "order-7"})

    handle(broker, TestRabbitBroker, send)
    attempts = tasks(recorder)

    assert [
        (trace.attrs["task.state"], trace.attrs["task.retries"], trace.status, trace.attrs["task.retry_reason"])
        for trace in attempts
    ] == [
        ("RETRY", 0, "ok", "ConnectionError('CRM is down for 7')"),
        ("RETRY", 1, "ok", "ConnectionError('CRM is down for 7')"),
    ]
    assert [[(error.type, error.handled) for error in trace.errors] for trace in attempts] == [
        [("builtins.ConnectionError", True)],
        [("builtins.ConnectionError", True)],
    ]

    assert [(event["event"], event.get("retries")) for event in without_worker_events(recorder.events)] == [
        ("started", 0),
        ("retried", 1),
        ("started", 1),
        ("retried", 2),
    ]


def test_a_nack_core_nats_drops_is_a_failure(recorder: Recorder) -> None:
    broker = NatsBroker()
    broker.subscriber("crm")(nack)

    async def send(tested: NatsBroker) -> None:
        await tested.publish({"id": 7}, "crm")

    handle(broker, TestNatsBroker, send)
    (task,) = tasks(recorder)

    assert (task.attrs["task.state"], task.status) == ("FAILURE", "error")


def test_a_rejected_message_fails_without_an_error(recorder: Recorder) -> None:
    broker = RabbitBroker()
    broker.subscriber("orders")(refuse)

    async def send(tested: RabbitBroker) -> None:
        await tested.publish({"id": 7}, "orders")

    handle(broker, TestRabbitBroker, send)
    (task,) = tasks(recorder)

    assert (task.attrs["task.state"], task.status, task.errors) == ("FAILURE", "error", [])

    assert [(event["event"], event.get("state")) for event in without_worker_events(recorder.events)] == [
        ("started", None),
        ("finished", "FAILURE"),
    ]


def test_ignored_handlers(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_IGNORE_TASKS", "*.housekeeping")
    broker = RedisBroker()
    broker.subscriber("cleanup")(housekeeping)

    async def send(tested: RedisBroker) -> None:
        await tested.publish({"id": 7}, "cleanup")

    handle(broker, TestRedisBroker, send)

    assert tasks(recorder) == []

    assert without_worker_events(recorder.events) == []


def test_a_handled_message_tells_the_agent_each_step(recorder: Recorder) -> None:
    broker = KafkaBroker()
    broker.subscriber("orders")(on_order_created)

    async def send(tested: KafkaBroker) -> None:
        with start_trace("http", "POST /orders") as request:
            await tested.publish({"id": 7}, "orders")
        recorder.traces.append(request)

    handle(broker, TestKafkaBroker, send)
    request = recorder.traces[-1]
    (publish,) = enqueues(request)
    task_id = publish.attrs["task.id"]

    # The in-memory broker runs the handler inside the publish: the agent takes the enqueue that comes after.
    assert without_worker_events(recorder.events) == [
        {
            "event": "started",
            "system": "kafka",
            "id": task_id,
            "time": IsPositiveInt,
            "name": HANDLER,
            "queue": "orders",
            "worker": IsStr(regex=rf"^{conf.get_config().service}@.+"),
            "retries": 0,
            "args": "({'id': 7},)",
            "kwargs": "{}",
        },
        {
            "event": "finished",
            "system": "kafka",
            "id": task_id,
            "time": IsPositiveInt,
            "worker": IsStr(regex=rf"^{conf.get_config().service}@.+"),
            "state": "SUCCESS",
        },
        {
            "event": "enqueued",
            "system": "kafka",
            "id": task_id,
            "time": IsPositiveInt,
            "name": "orders",
            "queue": "orders",
            "trace_id": request.id,
            "span_id": publish.id,
            "args": "({'id': 7},)",
            "kwargs": "{}",
        },
    ]


def test_a_broker_that_starts_says_its_app_is_a_worker(recorder: Recorder) -> None:
    broker = RedisBroker()
    broker.subscriber("orders")(on_order_created)

    async def start(tested: RedisBroker) -> None:
        # The in-memory broker replaces start(): what a broker's own start() ends with runs here.
        await BrokerUsecase.start(tested)

    handle(broker, TestRedisBroker, start)
    # An earlier test's heartbeat threads still beat, into whichever writer is current.
    beats = [event for event in recorder.events if event["event"] == "heartbeat" and event["system"] == "redis"]

    assert beats[0] == {
        "event": "heartbeat",
        "system": "redis",
        "id": "",
        "time": IsPositiveInt,
        "worker": IsStr(regex=rf"^{conf.get_config().service}@.+"),
        "ids": [],
    }


def test_arguments_are_left_out_when_asked(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    setenv(monkeypatch, "TRACEPANEL_CAPTURE_TASK_ARGS", "false")
    broker = KafkaBroker()
    broker.subscriber("orders")(on_order_created)

    async def send(tested: KafkaBroker) -> None:
        with start_trace("http", "POST /orders"):
            await tested.publish({"id": 7}, "orders")

    handle(broker, TestKafkaBroker, send)
    (task,) = tasks(recorder)

    assert {"task.args", "task.kwargs", "task.result"} & task.attrs.keys() == set()

    assert [{"args", "kwargs"} & event.keys() for event in without_worker_events(recorder.events)] == [set()] * 3


def test_faststream_says_in_the_hello_that_it_sends_task_events() -> None:
    assert "live_tasks" in agent.SENDS


def test_the_agent_lists_a_message_running_until_its_handler_ends(store: AgentStore) -> None:
    live(store)
    writer = store.writer()
    set_writer(writer)
    broker = KafkaBroker()
    release = asyncio.Event()

    @broker.subscriber("orders")
    async def waits(body: dict[str, int]) -> None:
        await release.wait()

    async def send(tested: KafkaBroker) -> None:
        with start_trace("http", "POST /orders"):
            publishing = asyncio.create_task(tested.publish({"id": 7}, "orders"))
        running = await asyncio.to_thread(live_when, store, lambda answer: bool(answer["tasks"]))
        worker = running["tasks"][0]["worker"]

        assert running["tasks"] == [
            IsPartialDict(
                system="kafka",
                name="tests.integrations.test_faststream.test_the_agent_lists_a_message_running_until_its_handler_ends"
                ".<locals>.waits",
                state="running",
                queue="orders",
                started_at=IsInt,
                retries=0,
                args="({'id': 7},)",
            )
        ]
        assert named(running, worker) == [
            {"name": worker, "system": "kafka", "service": IsStr, "last_seen": IsPositiveInt, "in_flight": 1}
        ]

        release.set()
        await publishing
        done = await asyncio.to_thread(live_when, store, lambda answer: not answer["tasks"])

        assert named(done, worker) == [IsPartialDict(name=worker, in_flight=0)]

    try:
        handle(broker, TestKafkaBroker, send)
    finally:
        set_writer(None)


# ---------- workflows ----------


def test_a_message_published_by_a_handler_follows_its_run(recorder: Recorder) -> None:
    broker = KafkaBroker()

    @broker.subscriber("orders")
    async def place(body: dict[str, int]) -> None:
        await broker.publish(body, "invoices")

    @broker.subscriber("invoices")
    async def invoice(body: dict[str, int]) -> None:
        await broker.publish(body, "emails")

    @broker.subscriber("emails")
    async def email(body: dict[str, int]) -> None:
        pass

    async def send(tested: KafkaBroker) -> None:
        with start_trace("http", "POST /orders"):
            await tested.publish({"id": 7}, "orders")

    handle(broker, TestKafkaBroker, send)
    # The in-memory broker runs each next step inside the publish: the last step's trace ends first.
    first, second, third = sorted(tasks(recorder), key=lambda trace: trace.start)
    (to_invoices,) = enqueues(first)
    (to_emails,) = enqueues(second)

    assert [trace.name.rsplit(".", 1)[-1] for trace in (first, second, third)] == ["place", "invoice", "email"]

    assert {"task.parent_id", "task.root_id"} & first.attrs.keys() == set()
    assert second.attrs == IsPartialDict(
        {
            "task.id": to_invoices.attrs["task.id"],
            "task.parent_id": first.attrs["task.id"],
            "task.root_id": first.attrs["task.id"],
        }
    )
    # The third step's root stays the first: the line started there.
    assert third.attrs == IsPartialDict(
        {
            "task.id": to_emails.attrs["task.id"],
            "task.parent_id": second.attrs["task.id"],
            "task.root_id": first.attrs["task.id"],
        }
    )

    assert [link.span_id for link in (*second.links, *third.links)] == [to_invoices.id, to_emails.id]


def test_a_rabbitmq_message_says_its_priority(recorder: Recorder) -> None:
    broker = RabbitBroker()
    broker.subscriber("orders")(on_order_created)

    async def send(tested: RabbitBroker) -> None:
        with start_trace("http", "POST /orders") as request:
            await tested.publish({"id": 7}, "orders", priority=5)
        recorder.traces.append(request)

    handle(broker, TestRabbitBroker, send)
    (task,) = tasks(recorder)
    (publish,) = enqueues(recorder.traces[-1])

    assert publish.attrs == IsPartialDict({"task.priority": 5})

    assert task.attrs == IsPartialDict({"task.priority": 5})


# ---------- under FastAPI ----------


# FastStream's own routers are deprecated for faststream-fastapi, and still work.
@pytest.mark.filterwarnings("ignore:The integration has been moved:DeprecationWarning")
def test_fastapi_router_handlers_are_named_after_their_function(recorder: Recorder) -> None:
    pytest.importorskip("fastapi")
    from fastapi import Depends, FastAPI  # noqa: PLC0415 (optional test group)
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from faststream.kafka.fastapi import KafkaRouter  # noqa: PLC0415

    import tracepanel.fastapi  # noqa: PLC0415

    def shop() -> str:
        return "shop"

    router = KafkaRouter()

    @router.subscriber("orders")
    async def on_order_placed(body: dict[str, int], name: str = Depends(shop)) -> str:
        return f"{name}:{body['id']}"

    app = FastAPI(debug=True)

    @app.post("/orders")
    async def place() -> dict[str, bool]:
        await router.broker.publish({"id": 7}, "orders")
        return {"ok": True}

    app.include_router(router)
    previous = conf._framework  # noqa: SLF001
    tracepanel.fastapi.install(app)

    async def post(tested: Any) -> None:  # noqa: ANN401
        del tested
        response = await asyncio.to_thread(TestClient(app).post, "/orders")
        assert response.status_code == 200

    try:
        handle(router.broker, TestKafkaBroker, post)
    finally:
        conf.use_source(previous)
    (task,) = tasks(recorder)
    (request,) = [trace for trace in recorder.traces if trace.kind == "http"]
    (publish,) = enqueues(request)

    assert task.name == (
        "tests.integrations.test_faststream.test_fastapi_router_handlers_are_named_after_their_function"
        ".<locals>.on_order_placed"
    )
    assert task.attrs == IsPartialDict({"task.system": "kafka", "task.state": "SUCCESS", "task.result": "'shop:7'"})

    assert task.links == [Link(kind="sent_by", trace_id=request.id, span_id=publish.id)]


def test_faststream_fastapi_apps_are_traced_too(recorder: Recorder) -> None:
    pytest.importorskip("faststream_fastapi")
    from fastapi import Depends, FastAPI  # noqa: PLC0415 (optional test group)
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from faststream_fastapi import FastStreamAPI  # noqa: PLC0415

    import tracepanel.fastapi  # noqa: PLC0415

    def shop() -> str:
        return "shop"

    broker = RabbitBroker()

    @broker.subscriber("orders")
    async def on_order_placed(body: dict[str, int], name: str = Depends(shop)) -> str:
        return f"{name}:{body['id']}"

    app = FastAPI(debug=True)

    @app.post("/orders")
    async def place() -> dict[str, bool]:
        await broker.publish({"id": 7}, "orders")
        return {"ok": True}

    previous = conf._framework  # noqa: SLF001
    tracepanel.fastapi.install(app)
    application = FastStreamAPI(broker, application=app)

    async def post(tested: Any) -> None:  # noqa: ANN401
        del tested
        response = await asyncio.to_thread(TestClient(application).post, "/orders")
        assert response.status_code == 200

    try:
        handle(broker, TestRabbitBroker, post)
    finally:
        conf.use_source(previous)
    (task,) = tasks(recorder)
    (request,) = [trace for trace in recorder.traces if trace.kind == "http"]
    (publish,) = enqueues(request)

    assert task.name == (
        "tests.integrations.test_faststream.test_faststream_fastapi_apps_are_traced_too.<locals>.on_order_placed"
    )
    assert task.attrs == IsPartialDict({"task.system": "rabbitmq", "task.state": "SUCCESS", "task.result": "'shop:7'"})

    assert task.links == [Link(kind="sent_by", trace_id=request.id, span_id=publish.id)]


def test_rpc_responses_are_no_task(recorder: Recorder) -> None:
    broker = NatsBroker()
    broker.subscriber("orders")(on_order_created)

    async def send(tested: NatsBroker) -> None:
        response = await tested.request({"id": 7}, "orders")
        assert await response.decode() == 7

    handle(broker, TestNatsBroker, send)

    assert [trace.name for trace in tasks(recorder)] == [HANDLER]


def test_a_message_a_profiled_request_publishes_asks_its_handler_to_profile(recorder: Recorder) -> None:
    broker = RabbitBroker()
    seen: list[dict[str, Any]] = []

    @broker.subscriber("reports")
    async def headers_of(body: dict[str, str], message: RabbitMessage) -> None:
        seen.append(dict(message.headers))

    async def send(tested: RabbitBroker) -> None:
        with start_trace("http", "POST /reports", profile=True):
            await tested.publish({"label": "asked"}, "reports")
        with start_trace("http", "GET /reports"):
            await tested.publish({"label": "plain"}, "reports")

    handle(broker, TestRabbitBroker, send)

    assert seen == [
        IsPartialDict({"tracepanel-profile": "1"}),
        IsPartialDict({"tracepanel-sent-by": IsStr}),
    ]
    assert "tracepanel-profile" not in seen[1]


def test_a_handler_whose_message_asks_for_a_profile_is_profiled(recorder: Recorder) -> None:
    # The in-memory broker runs the handler inside the publish, in the publisher's profile: here the message comes
    # from outside any trace, as a broker delivers it to a worker.
    broker = KafkaBroker()

    @broker.subscriber("reports")
    async def crunch(body: dict[str, str]) -> None:
        busy()

    async def send(tested: KafkaBroker) -> None:
        await tested.publish({"label": "asked"}, "reports", headers={"tracepanel-profile": "1"})
        await tested.publish({"label": "plain"}, "reports")

    handle(broker, TestKafkaBroker, send)

    assert {str(trace.attrs["task.args"]): trace.profile is not None for trace in tasks(recorder)} == {
        "({'label': 'asked'},)": True,
        "({'label': 'plain'},)": False,
    }
