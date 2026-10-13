"""Redis used directly: every command of redis-py (and its fork valkey-py) as a `redis` span, sync and async.

Redis is a datastore here, not a cache: queues, counters, locks and sessions live in it as often as cached values.
A cache on Redis is a `cache` span (with its hit or miss) whose Redis commands are its children.

A pipeline is one span: its commands go to the server together. Commands a library sends for the app (a cache's, a task
queue's, a channel layer's) are recorded too, under that library's span (cache.get, task.enqueue, channel.send): the
agent finds issues on that span, and counts the commands as Redis's load.
"""

import functools
import importlib
import sys
import time
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from tracepanel.core.callers import Callers
from tracepanel.core.context import blocking_attrs, current_trace, now_us, record_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs
from tracepanel.core.patching import patch
from tracepanel.core.services import location

CALLERS = Callers(
    own=__name__,
    libraries=("redis", "valkey", "fakeredis"),
    # Only tracepanel's own calls are left out. A cache's, a task queue's or a channel layer's commands are recorded
    # under their cache.get / task.enqueue / channel.send span: Redis's load, with what asked for it.
    layers=("tracepanel.", "opentelemetry."),
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


def _attrs(client: Any, command: str, key: str | int | None) -> Attrs:  # noqa: ANN401
    where = _where(client)
    namespace = where.get("db.namespace")
    return {
        "db.system": "redis",
        "db.operation": command,
        "redis.key": key,
        "redis.alias": f"redis/{namespace}" if namespace else "redis",
        **where,
        "code.origin": origin(),
    }


def _command(args: tuple[Any, ...]) -> tuple[str, str | int | None]:
    """The command's name (`GET`, `HSET`) and its key."""
    name = args[0].decode() if args and isinstance(args[0], bytes) else str(args[0]) if args else ""
    key = args[1] if len(args) > 1 else None
    if isinstance(key, bytes):
        key = key.decode(errors="replace")
    return name.upper(), key if key is None or isinstance(key, (str, int)) else str(key)


def _record(  # noqa: PLR0913, PLR0917
    client: Any,  # noqa: ANN401
    command: str,
    key: str | int | None,
    start: int,
    counter: int,
    status: str,
    blocking: Attrs | None = None,
) -> None:
    duration = (time.perf_counter_ns() - counter) // 1000
    # A pipeline's key is how many commands it sent: its name is the command alone.
    name = f"{command} {key}" if key is not None and command != "PIPELINE" else command
    attrs = {**_attrs(client, command, key), **(blocking or {})}
    record_span("redis", name, start=start, duration=duration, attrs=attrs, status=status)


def _run(client: Any, op: str, key: str | int | None, send: Callable[[], Any]) -> Any:  # noqa: ANN401
    start, counter, status, result = now_us(), time.perf_counter_ns(), "ok", None
    # The synchronous client, called on an event loop's thread, blocks it (redis.asyncio's doesn't).
    blocking = blocking_attrs()
    try:
        result = send()
    except Exception:
        status = "error"
        raise
    else:
        return result
    finally:
        _record(client, op, key, start, counter, status, blocking)


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
        _record(client, op, key, start, counter, status)


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
            return await _run_async(self, "PIPELINE", size, lambda: method(self, *args, **kwargs))

        return traced_async

    @functools.wraps(method)
    def traced(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None or not CALLERS.app_called(sys._getframe(0)):  # noqa: SLF001
            return method(self, *args, **kwargs)
        size = len(getattr(self, "command_stack", None) or ())
        return _run(self, "PIPELINE", size, lambda: method(self, *args, **kwargs))

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
