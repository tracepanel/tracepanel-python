"""MongoDB: every command of pymongo (MongoClient and AsyncMongoClient, and Motor on top of it) as a `mongodb` span.

Through pymongo's own command monitoring (a CommandListener registered for every client), not a patch: the listener
hears each command start, succeed or fail, in the thread or task that sent it. It applies to the clients created after
it is registered, which is when the app imports pymongo (or at startup, under `tracepanel run`).

A span is the command (`find users`), the database it ran in, the server and the line of the app that sent it. As with
SQL, the command's shape is its `db.statement` (`{"find": "users", "filter": {"email": "?"}}`), its values are
`db.params` (kept on this machine, and only with `capture_sql_params`) and `db.params_hash`: the agent tells repeats (an
N+1 of `find` per item) from duplicates (the same `find` with the same values) by them. Handshakes, authentication and
session bookkeeping are left out.
"""

import json
import threading
from collections.abc import Mapping
from typing import Any

from tracepanel.core import conf, sql
from tracepanel.core.context import blocking_attrs, current_trace, now_us, record_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, Scalar

MARKER = "_tracepanel"
# What a driver sends for itself, not for the app.
OWN = frozenset(
    {
        "hello",
        "ismaster",
        "ping",
        "saslstart",
        "saslcontinue",
        "authenticate",
        "getnonce",
        "endsessions",
        "buildinfo",
        "killcursors",
        "getlasterror",
    }
)
# Fields of a command that are the driver's, not the query's.
NOISE = frozenset(
    {"lsid", "$clusterTime", "$db", "txnNumber", "$readPreference", "autocommit", "startTransaction", "signature"}
)
# Where a command carries the app's values: these are shown as `?`, the rest (the collection, limit, sort, projection)
# as sent.
VALUES = frozenset({"filter", "query", "documents", "updates", "deletes", "pipeline", "q", "u", "update"})
MAX_STATEMENT = 2000
MAX_PARAMS = 200
# Commands started and not answered yet, at most this many (a client that never answers can't grow it for ever).
MAX_PENDING = 1000


def _collection(name: str, command: dict[str, Any]) -> str | None:
    """The collection a command works on: its own value (`{"find": "users"}`), or a getMore's `collection`."""
    value = command.get(name)
    if isinstance(value, str):
        return value
    collection = command.get("collection")
    return collection if isinstance(collection, str) else None


def _scalar(value: object) -> Scalar:
    return value if value is None or isinstance(value, (bool, int, float, str)) else str(value)


def _shape(value: object, values: list[Scalar]) -> object:
    """A value with the app's values as `?` (collected in order): a list of plain values is one `?`, as SQL's IN."""
    if isinstance(value, Mapping):
        return {str(key): _shape(item, values) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if all(not isinstance(item, (Mapping, list, tuple)) for item in value):
            values.extend(_scalar(item) for item in value)
            return ["?"]
        return [_shape(item, values) for item in value]
    values.append(_scalar(value))
    return "?"


def _statement(command: Mapping[str, Any]) -> tuple[str, list[Scalar]]:
    """The command's shape, without the driver's own fields and cut short, and its values."""
    values: list[Scalar] = []
    shaped = {
        key: _shape(value, values) if key in VALUES else value for key, value in command.items() if key not in NOISE
    }
    text = json.dumps(shaped, default=str, separators=(", ", ": "))
    return (text if len(text) <= MAX_STATEMENT else f"{text[: MAX_STATEMENT - 1]}…"), values


def _values(values: list[Scalar]) -> Attrs:
    """`db.params_hash` always (what tells duplicates), `db.params` when the settings keep values."""
    found: Attrs = {"db.params_hash": sql.params_hash(tuple(values))}
    if values and len(values) <= MAX_PARAMS and conf.get_config().capture_sql_params:
        found["db.params"] = values
    return found


def _rows(reply: object) -> int | None:
    """How many documents a find, an aggregate or a getMore returned."""
    cursor = reply.get("cursor") if isinstance(reply, dict) else None
    if not isinstance(cursor, dict):
        return None
    batch = cursor.get("firstBatch", cursor.get("nextBatch"))
    return len(batch) if isinstance(batch, list) else None


class _Listener:
    """A pymongo CommandListener: the attributes of a command when it starts, its span when it ends."""

    def __init__(self) -> None:
        self._pending: dict[tuple[object, int], tuple[int, str, Attrs]] = {}
        self._lock = threading.Lock()

    def started(self, event: Any) -> None:  # noqa: ANN401 (pymongo.monitoring.CommandStartedEvent)
        name = str(event.command_name)
        if name.lower() in OWN or current_trace() is None:
            return
        command = dict(event.command)
        statement, values = _statement(command)
        attrs: Attrs = {
            "db.system": "mongodb",
            "db.operation": name,
            "db.namespace": str(event.database_name),
            "db.statement": statement,
            **_values(values),
            "code.origin": origin(),
            **blocking_attrs(),
        }
        collection = _collection(name, command)
        if collection:
            attrs["db.collection.name"] = collection
        host, port = (*tuple(event.connection_id), None, None)[:2]
        if host:
            attrs["server.address"] = str(host)
        if port:
            attrs["server.port"] = int(port)
        title = f"{name} {collection}" if collection else name
        with self._lock:
            if len(self._pending) < MAX_PENDING:
                self._pending[(event.connection_id, event.request_id)] = (now_us(), title, attrs)

    def _finish(self, event: Any, status: str, extra: Attrs) -> None:  # noqa: ANN401
        with self._lock:
            found = self._pending.pop((event.connection_id, event.request_id), None)
        if found is None:
            return
        start, title, attrs = found
        record_span(
            "mongodb",
            title,
            start=start,
            duration=int(event.duration_micros),
            attrs={**attrs, **extra},
            status=status,
        )

    def succeeded(self, event: Any) -> None:  # noqa: ANN401 (pymongo.monitoring.CommandSucceededEvent)
        rows = _rows(event.reply)
        self._finish(event, "ok", {} if rows is None else {"db.response.returned_rows": rows})

    def failed(self, event: Any) -> None:  # noqa: ANN401 (pymongo.monitoring.CommandFailedEvent)
        failure: dict[str, object] = event.failure if isinstance(event.failure, dict) else {}
        self._finish(event, "error", {"error.type": str(failure.get("codeName") or "MongoError")})


def install() -> None:
    """Listen to every client's commands; a no-op without pymongo, or a second time."""
    try:
        from pymongo import monitoring  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    if getattr(monitoring, MARKER, False):
        return
    listener = type("TracepanelCommandListener", (_Listener, monitoring.CommandListener), {})()
    monitoring.register(listener)
    setattr(monitoring, MARKER, True)
