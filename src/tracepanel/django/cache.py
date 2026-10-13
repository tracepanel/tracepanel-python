"""Cache calls: every backend created through django.core.cache.caches is instrumented."""

import functools
from collections.abc import Callable, Sized
from typing import TYPE_CHECKING

from django.core.cache import CacheHandler, caches
from django.core.cache.backends.base import BaseCache

from tracepanel.core.context import blocking_attrs, current_trace, start_span
from tracepanel.core.frames import origin
from tracepanel.core.patching import patch, patched
from tracepanel.django import services as django_services

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs

OPERATIONS = ("get", "set", "add", "delete", "get_many", "set_many", "delete_many", "incr", "decr", "touch", "has_key")
MARKER = "_tracepanel"


def _key(op: str, args: tuple[object, ...]) -> str | int | None:
    if not args:
        return None
    first = args[0]
    if op.endswith("_many") and isinstance(first, Sized):
        return len(first)
    return str(first)


def _name(op: str, key: object) -> str:
    """`get user:1`, `get_many (3 keys)`: the operation and its key, as a query is its statement and table."""
    if isinstance(key, int):
        return f"{op} ({key} {'key' if key == 1 else 'keys'})"
    return f"{op} {key}" if key is not None else op


def _wrap(alias: str, op: str, method: Callable[..., object], service: "Attrs") -> Callable[..., object]:
    @functools.wraps(method)
    def wrapped(*args: object, **kwargs: object) -> object:
        if current_trace() is None:
            return method(*args, **kwargs)
        attrs: Attrs = {
            "cache.library": "django",
            "cache.alias": alias,
            **service,
            "cache.op": op,
            "cache.key": _key(op, args),
            "code.origin": origin(),
            # Django's async API (aget…) runs these in a thread: called on the event loop, they block it.
            **blocking_attrs(),
        }
        # Open while the backend works: its own calls (Redis commands, memcached) land inside.
        with start_span("cache", _name(op, attrs["cache.key"]), attrs) as span:
            result = method(*args, **kwargs)
            if span is not None and op == "get":
                default = args[1] if len(args) > 1 else kwargs.get("default")
                span.attrs["cache.hit"] = result is not default
            return result

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
