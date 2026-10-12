import asyncio
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from dirty_equals import IsStr

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
    return [span for span in trace.spans if span.kind == "cache"]


def test_each_command_is_a_span(port: int) -> None:
    client = redis.Redis(port=port, db=1)

    def work() -> None:
        client.set("user:1", "a")
        client.get("user:1")
        client.get("user:2")

    trace = traced(work)

    assert [span.name for span in commands(trace)] == ["redis.set", "redis.get", "redis.get"]
    assert commands(trace)[1].attrs == {
        "cache.system": "redis",
        "cache.alias": "redis/1",
        "cache.op": "get",
        "cache.key": "user:1",
        "cache.hit": True,
        "server.address": "localhost",
        "server.port": port,
        "db.namespace": "1",
        "code.origin": ORIGIN,
    }

    assert commands(trace)[2].attrs["cache.hit"] is False


def test_a_pipeline_is_one_span(port: int) -> None:
    client = redis.Redis(port=port)

    def work() -> None:
        with client.pipeline() as pipe:
            pipe.set("a", 1)
            pipe.incr("a")
            pipe.get("a")
            assert pipe.execute() == [True, 2, b"2"]

    trace = traced(work)

    assert [(span.name, span.attrs["cache.key"]) for span in commands(trace)] == [("redis.pipeline", 3)]


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

    assert [span.name for span in commands(trace)] == ["redis.set", "redis.pipeline"]
    assert commands(trace)[0].attrs["code.origin"] == ORIGIN


def test_a_failing_command_is_an_error(port: int) -> None:
    client = redis.Redis(port=port)

    def work() -> None:
        client.set("text", "a")
        with pytest.raises(redis.ResponseError):
            client.incr("text")

    assert [(span.name, span.status) for span in commands(traced(work))] == [
        ("redis.set", "ok"),
        # What redis-py sends for incr().
        ("redis.incrby", "error"),
    ]


# ---------- what records its own Redis calls ----------


def test_django_s_redis_cache_is_left_to_django(port: int) -> None:
    from django.core.cache.backends.redis import RedisCache  # noqa: PLC0415

    cache = RedisCache(f"redis://127.0.0.1:{port}/2", {})

    def work() -> None:
        cache.set("k", "v")
        assert cache.get("k") == "v"

    assert commands(traced(work)) == []


def test_an_rq_enqueue_is_left_to_rq(port: int) -> None:
    rq = pytest.importorskip("rq")
    queue = rq.Queue("default", connection=redis.Redis(port=port))

    trace = traced(lambda: queue.enqueue("builtins.print", "hi"))

    assert commands(trace) == []


def calling_from(module: str) -> Callable[[redis.Redis], Any]:
    namespace: dict[str, Any] = {"__name__": module}
    exec("def run(client):\n    return client.get('k')\n", namespace)  # noqa: S102
    return namespace["run"]


@pytest.mark.parametrize("module", ["kombu.transport.redis", "celery.backends.redis", "channels_redis.core"])
def test_task_queues_and_channel_layers_are_left_out(port: int, module: str) -> None:
    client = redis.Redis(port=port)

    assert commands(traced(lambda: calling_from(module)(client))) == []


def test_outside_a_trace_it_only_runs(port: int) -> None:
    client = redis.Redis(port=port)
    client.set("plain", "1")

    assert client.get("plain") == b"1"
