"""Cache calls: every backend created through django.core.cache.caches is instrumented."""

import functools
import time
from collections.abc import Callable, Sized
from typing import TYPE_CHECKING

from django.core.cache import CacheHandler, caches
from django.core.cache.backends.base import BaseCache

from tracepanel.core.context import current_trace, now_us, record_span
from tracepanel.core.patching import patch, patched
from tracepanel.django import services as django_services

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs

OPERATIONS = ("get", "set", "add", "delete", "get_many", "set_many", "delete_many", "incr", "decr", "touch", "has_key")
MARKER = "_tracepanel"
_MISSING = object()


def _key(op: str, args: tuple[object, ...]) -> str | int | None:
    if not args:
        return None
    first = args[0]
    if op.endswith("_many") and isinstance(first, Sized):
        return len(first)
    return str(first)


def _wrap(alias: str, op: str, method: Callable[..., object], service: "Attrs") -> Callable[..., object]:
    @functools.wraps(method)
    def wrapped(*args: object, **kwargs: object) -> object:
        if current_trace() is None:
            return method(*args, **kwargs)
        start = now_us()
        started = time.perf_counter_ns()
        status = "ok"
        result: object = _MISSING
        try:
            result = method(*args, **kwargs)
        except Exception:
            status = "error"
            raise
        else:
            return result
        finally:
            attrs: Attrs = {"cache.alias": alias, **service, "cache.op": op, "cache.key": _key(op, args)}
            if op == "get" and result is not _MISSING:
                default = args[1] if len(args) > 1 else kwargs.get("default")
                attrs["cache.hit"] = result is not default
            duration = (time.perf_counter_ns() - started) // 1000
            record_span("cache", f"cache.{op}", start=start, duration=duration, attrs=attrs, status=status)

    return wrapped


def instrument(backend: BaseCache, alias: str) -> BaseCache:
    if getattr(backend, MARKER, False):
        return backend
    service = django_services.for_cache(backend)
    for op in OPERATIONS:
        method = getattr(backend, op, None)
        if callable(method):
            setattr(backend, op, _wrap(alias, op, method, service))
    setattr(backend, MARKER, True)
    return backend


def install() -> None:
    if patched(CacheHandler, "create_connection"):
        return
    original = CacheHandler.create_connection

    @functools.wraps(original)
    def create_connection(self: CacheHandler, alias: str) -> BaseCache:
        return instrument(original(self, alias), alias)

    patch(CacheHandler, "create_connection", create_connection)
    # Backends created before tracepanel was ready: instrument them without creating new ones.
    for alias in caches.settings:
        backend = getattr(getattr(caches, "_connections", None), alias, None)
        if isinstance(backend, BaseCache):
            instrument(backend, alias)
