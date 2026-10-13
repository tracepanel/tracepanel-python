import asyncio
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from dirty_equals import IsPartialDict, IsStr
from jsonschema import Draft202012Validator

pytest.importorskip("redis")
fakeredis = pytest.importorskip("fakeredis")

import redis  # noqa: E402 (optional test group)
import redis.asyncio  # noqa: E402

from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Span, Trace  # noqa: E402
from tracepanel.integrations import redis as integration  # noqa: E402
from tracepanel.storage.transport import complete  # noqa: E402

integration.install()

ORIGIN = IsStr(regex=r"tests/integrations/test_redis\.py:\d+ in work")


@pytest.fixture(scope="module")
def port() -> Iterator[int]:
    """A Redis server in this process (fakeredis' TCP server): the clients talk to it over a socket."""
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()


def traced(work: Callable[[], object]) -> Trace:
    with start_trace("http", "GET /") as trace:
        work()
    return complete(trace)


def commands(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "redis"]


def test_each_command_is_a_span(port: int, validator: Draft202012Validator) -> None:
    client = redis.Redis(port=port, db=1)

    def work() -> None:
        client.set("user:1", "a")
        client.get("user:1")
        client.get("user:2")

    trace = traced(work)
    validator.validate(trace.to_dict())

    assert [span.name for span in commands(trace)] == ["SET user:1", "GET user:1", "GET user:2"]
    assert commands(trace)[1].attrs == {
        "db.system": "redis",
        "db.operation": "GET",
        "redis.key": "user:1",
        "redis.alias": "redis/1",
        "server.address": "localhost",
        "server.port": port,
        "db.namespace": "1",
        "code.origin": ORIGIN,
    }


def test_a_pipeline_is_one_span(port: int) -> None:
    client = redis.Redis(port=port)

    def work() -> None:
        with client.pipeline() as pipe:
            pipe.set("a", 1)
            pipe.incr("a")
            pipe.get("a")
            assert pipe.execute() == [True, 2, b"2"]

    trace = traced(work)

    assert [(span.name, span.attrs["redis.key"]) for span in commands(trace)] == [("PIPELINE", 3)]


def test_async_commands_and_pipeline(port: int) -> None:
    async def work() -> None:
        client = redis.asyncio.Redis(port=port)
        try:
            await client.set("b", "x")
            async with client.pipeline() as pipe:
                pipe.get("b")
                pipe.get("c")
                await pipe.execute()
        finally:
            await client.aclose()

    trace = traced(lambda: asyncio.run(work()))

    assert [span.name for span in commands(trace)] == ["SET b", "PIPELINE"]
    assert commands(trace)[0].attrs["code.origin"] == ORIGIN


def test_a_failing_command_is_an_error(port: int) -> None:
    client = redis.Redis(port=port)

    def work() -> None:
        client.set("text", "a")
        with pytest.raises(redis.ResponseError):
            client.incr("text")

    assert [(span.name, span.status) for span in commands(traced(work))] == [
        ("SET text", "ok"),
        # What redis-py sends for incr().
        ("INCRBY text", "error"),
    ]


# ---------- Redis under a library: the library's span, its commands inside ----------


def test_a_django_cache_on_redis_has_its_commands_inside(port: int) -> None:
    from django.core.cache.backends.redis import RedisCache  # noqa: PLC0415

    from tracepanel.django.cache import instrument  # noqa: PLC0415

    cache = instrument(RedisCache(f"redis://127.0.0.1:{port}/2", {}), "default")

    def work() -> None:
        cache.set("k", "v")
        assert cache.get("k") == "v"

    trace = traced(work)
    by_id = {span.id: span for span in trace.spans}
    parents = [(by_id[str(span.parent_id)].name, span.attrs["db.operation"]) for span in commands(trace)]

    assert parents == [("set k", "SET"), ("get k", "GET")]
    assert by_id[str(commands(trace)[1].parent_id)].attrs == IsPartialDict(
        {"cache.library": "django", "cache.op": "get", "cache.hit": True}
    )


def test_an_rq_enqueue_has_its_commands_inside(port: int) -> None:
    rq = pytest.importorskip("rq")
    from tracepanel.integrations import rq as rq_integration  # noqa: PLC0415

    rq_integration.install()
    queue = rq.Queue("default", connection=redis.Redis(port=port))

    trace = traced(lambda: queue.enqueue("builtins.print", "hi"))
    enqueue = next(span for span in trace.spans if span.kind == "task.enqueue")

    assert commands(trace) != []
    assert {span.parent_id for span in commands(trace)} == {enqueue.id}


def calling_from(module: str) -> Callable[[redis.Redis], Any]:
    namespace: dict[str, Any] = {"__name__": module}
    exec("def run(client):\n    return client.get('k')\n", namespace)  # noqa: S102
    return namespace["run"]


@pytest.mark.parametrize("module", ["tracepanel.storage.agent", "opentelemetry.exporter.otlp"])
def test_tracepanel_s_own_commands_are_left_out(port: int, module: str) -> None:
    client = redis.Redis(port=port)

    assert commands(traced(lambda: calling_from(module)(client))) == []


def test_outside_a_trace_it_only_runs(port: int) -> None:
    client = redis.Redis(port=port)
    client.set("plain", "1")

    assert client.get("plain") == b"1"


def test_the_sync_client_on_an_event_loop_blocks_it_the_async_one_doesnt(port: int) -> None:
    sync, asynchronous = redis.Redis(port=port), redis.asyncio.Redis(port=port)

    async def view() -> Trace:
        with start_trace("http", "GET /") as trace:
            sync.get("user:1")
            await asynchronous.get("user:1")
            await asynchronous.aclose()
        return trace

    trace = complete(asyncio.run(view()))

    assert [span.attrs.get("code.blocks_event_loop") for span in commands(trace)] == [True, None]
