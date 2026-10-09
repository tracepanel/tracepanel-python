"""What sits behind each outgoing call: the system (postgresql, redis, smtp…) and where it runs.

Attributes follow OpenTelemetry semantic conventions (`db.system`, `server.address`, `server.port`), so the local
Services page and the cloud service map read the same thing. Credentials never leave the URL parser: only scheme,
host, port and the database name or number are kept.
"""

import functools
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tracepanel.core.model import Attrs, Span, Trace

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


@dataclass(frozen=True, slots=True)
class Peer:
    """A system this process talks to; `alias` is the Django alias (DATABASES / CACHES) when there is one."""

    kind: str  # database, cache, mail, queue, http
    system: str
    address: str | None
    port: int | None
    namespace: str | None
    alias: str | None

    @property
    def id(self) -> str:
        where = f"{self.address}:{self.port}" if self.port else self.address or ""
        return "/".join(part for part in (f"{self.kind}:{self.system}", where, self.namespace or "") if part)


@dataclass(slots=True)
class Usage:
    calls: int = 0
    duration: int = 0
    errors: int = 0


def _text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _port(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _peer(span: Span) -> Peer | None:  # noqa: PLR0911 (one return per kind of peer)
    attrs = span.attrs
    address, port = _text(attrs.get("server.address")), _port(attrs.get("server.port"))
    if span.kind == "sql":
        system = _text(attrs.get("db.system")) or _text(attrs.get("db.vendor")) or "sql"
        alias = _text(attrs.get("db.alias")) or "default"
        # In-memory SQLite has neither a server nor a file: the alias tells two of them apart.
        namespace = _text(attrs.get("db.namespace")) or (None if address else alias)
        return Peer("database", system, address, port, namespace, alias)
    if span.kind == "cache":
        alias = _text(attrs.get("cache.alias")) or "default"
        # In-process caches have no address: the alias tells two of them apart.
        namespace = _text(attrs.get("db.namespace")) if address else alias
        return Peer("cache", _text(attrs.get("cache.system")) or "cache", address, port, namespace, alias)
    if span.kind == "mail":
        return Peer("mail", _text(attrs.get("mail.system")) or "mail", address, port, None, None)
    if span.kind in {"task.enqueue", "channel.send"} and attrs.get("messaging.system"):
        return Peer("queue", str(attrs["messaging.system"]), address, port, None, None)
    if span.kind == "http.client" and address:
        return Peer("http", _text(attrs.get("url.scheme")) or "http", address, port, None, None)
    if system := _text(attrs.get("db.system")):
        # Any other database a span names (an OpenTelemetry redis or MongoDB span): by the semantic conventions.
        return Peer("database", system, address, port, _text(attrs.get("db.namespace")), None)
    return None


def dependencies(trace: Trace) -> dict[tuple[Peer, str], Usage]:
    """Calls per (peer, direction): `out` for what the trace called, `in` for the broker a worker consumed from."""
    usage: dict[tuple[Peer, str], Usage] = {}
    for span in trace.spans:
        peer = _peer(span)
        if peer is None:
            continue
        item = usage.setdefault((peer, "out"), Usage())
        item.calls += 1
        item.duration += span.duration
        item.errors += span.status == "error"
    system = trace.attrs.get("messaging.system")
    if trace.kind == "task" and system:
        broker = Peer(
            "queue",
            str(system),
            _text(trace.attrs.get("server.address")),
            _port(trace.attrs.get("server.port")),
            None,
            None,
        )
        usage[broker, "in"] = Usage(calls=1, duration=_port(trace.attrs.get("task.queue_wait")) or 0)
    return usage
