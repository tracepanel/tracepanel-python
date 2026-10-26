"""What sits behind Django's database connections, cache backends and file storages: the core's service attributes."""

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


# File storages by module: Django's own, then django-storages' backends (S3 under its old module name too).
_STORAGE_SYSTEMS = (
    ("django.core.files.storage.filesystem", "filesystem"),
    ("django.contrib.staticfiles.storage", "filesystem"),
    ("django.core.files.storage.memory", "memory"),
    ("storages.backends.s3", "s3"),
    ("storages.backends.s3boto3", "s3"),
    ("storages.backends.gcloud", "gcs"),
    ("storages.backends.azure_storage", "azure"),
    ("storages.backends.dropbox", "dropbox"),
    ("storages.backends.sftpstorage", "sftp"),
    ("storages.backends.ftp", "ftp"),
    ("storages.backends.apache_libcloud", "libcloud"),
)


def storage_system(storage: object) -> str:
    """`filesystem`, `s3`, `gcs`…: the module of the storage's class or of its nearest known base, else its name."""
    for kind in type(storage).__mro__:
        for module, system in _STORAGE_SYSTEMS:
            if kind.__module__ == module or kind.__module__.startswith(f"{module}."):
                return system
    return type(storage).__name__.lower()
