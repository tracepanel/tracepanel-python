"""What sits behind each outgoing call: the system (postgresql, redis, smtp…) and where it runs.

Attributes follow OpenTelemetry semantic conventions (`db.system`, `server.address`, `server.port`): the agent's
Services page draws the project's services from them. Credentials never leave the URL parser: only scheme,
host, port and the database name or number are kept.
"""

import functools
import re
from pathlib import Path
from urllib.parse import urlsplit

from tracepanel.core.model import Attrs

DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "oracle": 1521,
    "redis": 6379,
    "memcached": 11211,
    "rabbitmq": 5672,
    "smtp": 25,
}

_BROKER_SYSTEMS = {
    "redis": "redis",
    "rediss": "redis",
    "sentinel": "redis",
    "amqp": "rabbitmq",
    "amqps": "rabbitmq",
    "pyamqp": "rabbitmq",
    "librabbitmq": "rabbitmq",
    "sqs": "sqs",
    "memory": "memory",
    "filesystem": "filesystem",
}

_HOST_PORT = re.compile(r"^\[?(?P<host>[^\]\s/]+?)\]?(?::(?P<port>\d+))?$")


def _endpoint(host: object, port: object, system: str) -> Attrs:
    """`server.address` / `server.port`; a local socket path is an address without a port."""
    address = str(host).strip() if host else ""
    if not address:
        return {}
    if address.startswith("/"):
        return {"server.address": address}
    number = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else DEFAULT_PORTS.get(system)
    return {"server.address": address, "server.port": number}


def location(value: str, system: str) -> Attrs:
    """Host, port and database number from `redis://user:pw@host:6379/1`, `host:11211` or `unix:/run/redis.sock`."""
    value = value.strip()
    if value.startswith("unix:"):
        return {"server.address": value.removeprefix("unix:").split("?", 1)[0].removeprefix("//")}
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme == "unix":
            return {"server.address": parts.path}
        attrs = _endpoint(parts.hostname, parts.port, system)
        number = parts.path.strip("/")
        if number:
            attrs["db.namespace"] = number
        return attrs
    match = _HOST_PORT.match(value)
    return _endpoint(match["host"], match["port"], system) if match else {}


@functools.cache
def database(vendor: str, host: str, port: str, name: str) -> Attrs:
    """`db.system`, `db.namespace` and the server of one DATABASES entry (cached: settings do not change)."""
    attrs: Attrs = {"db.system": vendor}
    if vendor == "sqlite":
        # The file is the database; ":memory:" and test databases have no file worth naming.
        attrs["db.namespace"] = Path(name).name if name and not name.startswith((":memory:", "file:")) else None
        return attrs
    attrs["db.namespace"] = name or None
    # An empty HOST means the local socket for PostgreSQL and MySQL.
    attrs.update(_endpoint(host or "localhost", port, vendor))
    return attrs


def for_smtp(host: object, port: object) -> Attrs:
    return {"mail.system": "smtp", **_endpoint(host, port, "smtp")}


@functools.cache
def for_broker(url: str) -> Attrs:
    """`messaging.system` and the broker's server from a Celery/Kombu broker URL."""
    first = (url or "amqp://").split(";", 1)[0]
    scheme = first.split("://", 1)[0].split("+", 1)[-1].lower()
    system = _BROKER_SYSTEMS.get(scheme, scheme)
    attrs: Attrs = {"messaging.system": system}
    if system in {"memory", "filesystem"}:
        return attrs
    server = location(first, system)
    server.pop("db.namespace", None)
    if not server and system == "rabbitmq":
        server = _endpoint("localhost", None, system)
    return {**attrs, **server}


# ---------- the dependencies of one trace (the edges of the service map) ----------
