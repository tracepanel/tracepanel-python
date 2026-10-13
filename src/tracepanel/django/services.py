"""What sits behind Django's database connections and cache backends, for the core's service attributes."""

from typing import Any

from tracepanel.core.model import Attrs
from tracepanel.core.services import database, location

# Cache backends by class name; third-party ones (django-redis, django-valkey) match by module.
_CACHE_SYSTEMS = {
    "RedisCache": "redis",
    "PyMemcacheCache": "memcached",
    "PyLibMCCache": "memcached",
    "MemcachedCache": "memcached",
    "LocMemCache": "locmem",
    "FileBasedCache": "file",
    "DatabaseCache": "database",
    "DummyCache": "dummy",
}


def for_connection(connection: Any) -> Attrs:  # noqa: ANN401 (any DB-API wrapper with settings_dict)
    settings = getattr(connection, "settings_dict", None) or {}
    return database(
        str(getattr(connection, "vendor", "")),
        str(settings.get("HOST") or ""),
        str(settings.get("PORT") or ""),
        str(settings.get("NAME") or ""),
    )


def cache_system(backend: object) -> str:
    kind = type(backend)
    if kind.__name__ in _CACHE_SYSTEMS and kind.__module__.startswith("django."):
        return _CACHE_SYSTEMS[kind.__name__]
    module = kind.__module__.split(".", 1)[0]
    for system in ("redis", "valkey", "memcache", "pymemcache"):
        if system in module:
            return "memcached" if "memcache" in system else system
    return _CACHE_SYSTEMS.get(kind.__name__, kind.__name__.lower())


def for_cache(backend: object) -> Attrs:
    system = cache_system(backend)
    attrs: Attrs = {"cache.system": system}
    servers = getattr(backend, "_servers", None) or getattr(backend, "_server", None)
    if isinstance(servers, str):
        servers = [servers]
    if system not in {"locmem", "file", "database", "dummy"} and servers:
        attrs.update(location(str(servers[0]), system))
    return attrs
