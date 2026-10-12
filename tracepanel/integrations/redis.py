"""Redis used directly: every command of redis-py (and its fork valkey-py) as a `cache` span, sync and async.

A pipeline is one span: its commands go to the server together. What tracepanel records already is left out, by who
called the client (tracepanel.core.callers): Django's cache (its RedisCache, django-redis), the task queues' brokers and
result backends (Celery and kombu, RQ, Dramatiq) and Channels' layer.
"""

import functools
import importlib
import sys
import time
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from tracepanel.core.callers import Callers
from tracepanel.core.context import current_trace, now_us, record_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs
from tracepanel.core.patching import patch
from tracepanel.core.services import location

CALLERS = Callers(
    own=__name__,
    libraries=("redis", "valkey", "fakeredis"),
    layers=(
        "tracepanel.",
        "django.core.cache.",
        "django_redis.",
        "django_valkey.",
        "kombu.",
        "celery.",
        "rq.",
        "dramatiq.",
        "channels_redis.",
        "opentelemetry.",
    ),
)


def _where(client: Any) -> Attrs:  # noqa: ANN401 (a Redis client, or its pipeline)
    pool = getattr(client, "connection_pool", None)
    kwargs: dict[str, Any] = getattr(pool, "connection_kwargs", None) or {}
    if "path" in kwargs:
        return location(f"unix:{kwargs['path']}", "redis")
    host, port, db = kwargs.get("host"), kwargs.get("port"), kwargs.get("db") or 0
    if host is None:
        return {}
    return location(f"redis://{host}:{port or 6379}/{db}" if db else f"redis://{host}:{port or 6379}", "redis")


def _attrs(client: Any, op: str, key: str | int | None) -> Attrs:  # noqa: ANN401
    where = _where(client)
    namespace = where.get("db.namespace")
    return {
        "cache.system": "redis",
        "cache.alias": f"redis/{namespace}" if namespace else "redis",
        "cache.op": op,
        "cache.key": key,
        **where,
        "code.origin": origin(),
    }


def _command(args: tuple[Any, ...]) -> tuple[str, str | int | None]:
    """The command's name (`get`, `hset`) and its key."""
    name = args[0].decode() if args and isinstance(args[0], bytes) else str(args[0]) if args else ""
    key = args[1] if len(args) > 1 else None
    if isinstance(key, bytes):
        key = key.decode(errors="replace")
    return name.lower(), key if key is None or isinstance(key, (str, int)) else str(key)


def _record(client: Any, op: str, key: str | int | None, start: int, counter: int, status: str, result: object) -> None:  # noqa: ANN401, PLR0913, PLR0917
    attrs = _attrs(client, op, key)
    if op == "get" and status == "ok":
        attrs["cache.hit"] = result is not None
    duration = (time.perf_counter_ns() - counter) // 1000
    record_span("cache", f"redis.{op}", start=start, duration=duration, attrs=attrs, status=status)


def _run(client: Any, op: str, key: str | int | None, send: Callable[[], Any]) -> Any:  # noqa: ANN401
    start, counter, status, result = now_us(), time.perf_counter_ns(), "ok", None
    try:
        result = send()
    except Exception:
        status = "error"
        raise
    else:
        return result
    finally:
        _record(client, op, key, start, counter, status, result)


async def _run_async(client: Any, op: str, key: str | int | None, send: Callable[[], Awaitable[Any]]) -> Any:  # noqa: ANN401
    start, counter, status, result = now_us(), time.perf_counter_ns(), "ok", None
    try:
        result = await send()
    except Exception:
        status = "error"
        raise
    else:
        return result
    finally:
        _record(client, op, key, start, counter, status, result)


def _command_wrapper(method: Callable[..., Any], *, asynchronous: bool) -> Callable[..., Any]:
    if asynchronous:

        @functools.wraps(method)
        async def traced_async(self: Any, *args: Any, **options: Any) -> Any:  # noqa: ANN401 (redis-py's signature)
            if current_trace() is None or not CALLERS.app_called(sys._getframe(0)):  # noqa: SLF001 (who called)
                return await method(self, *args, **options)
            op, key = _command(args)
            return await _run_async(self, op, key, lambda: method(self, *args, **options))

        return traced_async

    @functools.wraps(method)
    def traced(self: Any, *args: Any, **options: Any) -> Any:  # noqa: ANN401
        if current_trace() is None or not CALLERS.app_called(sys._getframe(0)):  # noqa: SLF001
            return method(self, *args, **options)
        op, key = _command(args)
        return _run(self, op, key, lambda: method(self, *args, **options))

    return traced


def _pipeline_wrapper(method: Callable[..., Any], *, asynchronous: bool) -> Callable[..., Any]:
    """A pipeline's execute(): one span, its key the number of commands sent."""
    if asynchronous:

        @functools.wraps(method)
        async def traced_async(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if current_trace() is None or not CALLERS.app_called(sys._getframe(0)):  # noqa: SLF001
                return await method(self, *args, **kwargs)
            size = len(getattr(self, "command_stack", None) or ())
            return await _run_async(self, "pipeline", size, lambda: method(self, *args, **kwargs))

        return traced_async

    @functools.wraps(method)
    def traced(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None or not CALLERS.app_called(sys._getframe(0)):  # noqa: SLF001
            return method(self, *args, **kwargs)
        size = len(getattr(self, "command_stack", None) or ())
        return _run(self, "pipeline", size, lambda: method(self, *args, **kwargs))

    return traced


def _wrap(module: ModuleType | None, client: str, pipeline: str, *, asynchronous: bool) -> None:
    if module is None:
        return
    for name, method, wrapper in (
        (client, "execute_command", _command_wrapper),
        (pipeline, "execute", _pipeline_wrapper),
    ):
        cls = getattr(module, name, None)
        if cls is not None and method in vars(cls):
            patch(cls, method, wrapper(vars(cls)[method], asynchronous=asynchronous))


def _optional(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def install() -> None:
    """Wrap redis-py and valkey-py when installed: the client, its cluster client and their pipelines."""
    for package, client in (("redis", "Redis"), ("valkey", "Valkey")):
        if _optional(package) is None:
            continue
        _wrap(_optional(f"{package}.client"), client, "Pipeline", asynchronous=False)
        _wrap(_optional(f"{package}.cluster"), f"{client}Cluster", "ClusterPipeline", asynchronous=False)
        _wrap(_optional(f"{package}.asyncio.client"), client, "Pipeline", asynchronous=True)
        _wrap(_optional(f"{package}.asyncio.cluster"), f"{client}Cluster", "ClusterPipeline", asynchronous=True)
