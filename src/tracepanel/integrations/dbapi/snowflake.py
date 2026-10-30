"""Snowflake: snowflake-connector-python, sync and asyncio (`snowflake.connector.aio`), 4.x and 5.x.

Opening a connection logs in over HTTPS, creates the session and checks certificates: seconds, on a cold start. It is
a `sql.connect` span, and every statement a `sql` span. The HTTP calls the connector makes for them (the login, the
queries' requests, their results' chunks, the certificate checks, its telemetry) are not spans of their own: their
time is the connection's or the statement's. From 5.0 a Rust core makes them, out of Python's sight; before 5.0 its own
copy of requests and urllib3 does (aiohttp for asyncio), which tracepanel.integrations.http_clients leaves out. The
app's own HTTP calls are recorded as ever, a statement running or not.

Classes by version: 4.x's SnowflakeConnection (connect() opens it, __init__ calls it) and SnowflakeCursorBase, in
`connection`, `cursor`, `aio._connection` and `aio._cursor`; 5.x's Connection (its __init__ opens it; the asyncio one's
connect()) and SnowflakeCursorBase, in `connection._connection`, `cursor._base` and their `aio` twins.
"""

import functools
from collections.abc import Callable
from types import ModuleType
from typing import Any

from tracepanel.core import sql
from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs
from tracepanel.core.patching import patch
from tracepanel.integrations.dbapi import _core

CLIENT = "snowflake"
SYSTEM = "snowflake"
_HTTPS = 443


def _execute(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """execute(command, params=None) before 5.0, execute(operation, parameters=None, *, params=None) from 5.0."""
    statement = args[0] if args else kwargs.get("command", kwargs.get("operation", ""))
    params = args[1] if len(args) > 1 else kwargs.get("params", kwargs.get("parameters"))
    return statement, _core.params_of(params), False


def _executemany(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """executemany(command, seqparams); from 5.0 executemany(operation, seq_of_parameters)."""
    return (args[0] if args else kwargs.get("command", kwargs.get("operation", ""))), None, True


_METHODS: dict[str, _core.Reads] = {"execute": _execute, "executemany": _executemany}


def _read(connection: Any, name: str) -> object:  # noqa: ANN401 (a Snowflake connection)
    """A connection's property; 5.x reads them from its Rust core, which may not have them yet."""
    try:
        return getattr(connection, name, None)
    except Exception:  # noqa: BLE001 (a property the connection can't answer is not the app's error)
        return None


def _attrs(connection: Any) -> Attrs:  # noqa: ANN401
    """Where a connection points: its host (`<account>.snowflakecomputing.com`) and database. No EXPLAIN."""
    port = _read(connection, "port")
    return _core.attrs(
        _core.Where(
            CLIENT,
            SYSTEM,
            str(_read(connection, "host") or ""),
            str(port or _HTTPS),
            str(_read(connection, "database") or ""),
        )
    )


def _cursor_attrs(cursor: Any) -> Attrs:  # noqa: ANN401
    return _attrs(cursor.connection)


def _opening(method: Callable[..., Any]) -> Callable[..., Any]:
    """4.x's connect(), 5.x's __init__: the connection opened, described once open."""

    @functools.wraps(method)
    def traced(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (the connector's own signature)
        return _core.opening(
            lambda: method(self, *args, **kwargs), client=CLIENT, system=SYSTEM, where=_core.described(self, _attrs)
        )

    return traced


def _opening_async(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def traced(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return await _core.opening_async(
            lambda: method(self, *args, **kwargs), client=CLIENT, system=SYSTEM, where=_core.described(self, _attrs)
        )

    return traced


def _ending(method: Callable[..., Any], statement: str) -> Callable[..., Any]:
    """commit() and rollback(): a statement each, on a cursor of the connection's (4.x) or in its core (5.x).

    Recorded whoever calls: SQLAlchemy records neither its COMMIT nor its pool's ROLLBACK when a connection goes back
    (both a round trip to Snowflake, on every request). The cursor's statement inside is this one.
    """

    @functools.wraps(method)
    def traced(self: Any) -> Any:  # noqa: ANN401
        return sql.run(lambda: method(self), statement, None, many=False, database=lambda: _attrs(self))

    return traced


def _ending_async(method: Callable[..., Any], statement: str) -> Callable[..., Any]:
    @functools.wraps(method)
    async def traced(self: Any) -> Any:  # noqa: ANN401
        return await _core.run_async(
            lambda: method(self), statement, None, many=False, db=lambda: _attrs(self), always=True
        )

    return traced


def _connection(name: str, method: str, *, asynchronous: bool) -> Callable[[ModuleType], None]:
    """Wrap the module's connection class where it defines them: the method that opens it, commit() and rollback()."""

    def wrap(module: ModuleType) -> None:
        cls = getattr(module, name, None)
        if not isinstance(cls, type):
            return
        if _core.defined_on(cls, method):
            opened = vars(cls)[method]
            patch(cls, method, _opening_async(opened) if asynchronous else _opening(opened))
        for ending, statement in (("commit", "COMMIT"), ("rollback", "ROLLBACK")):
            if _core.defined_on(cls, ending):
                ended = vars(cls)[ending]
                patch(cls, ending, _ending_async(ended, statement) if asynchronous else _ending(ended, statement))

    return wrap


def _cursor(*, asynchronous: bool) -> Callable[[ModuleType], None]:
    def wrap(module: ModuleType) -> None:
        cls = getattr(module, "SnowflakeCursorBase", None)
        if isinstance(cls, type):
            _core.wrap(cls, _METHODS, db=_cursor_attrs, asynchronous=asynchronous)

    return wrap


def install() -> None:
    """Wrap the connector once the app imports it, whichever version."""
    connector = "snowflake.connector"
    # 4.x
    when_imported(f"{connector}.connection", _connection("SnowflakeConnection", "connect", asynchronous=False))
    when_imported(f"{connector}.cursor", _cursor(asynchronous=False))
    when_imported(f"{connector}.aio._connection", _connection("SnowflakeConnection", "connect", asynchronous=True))
    when_imported(f"{connector}.aio._cursor", _cursor(asynchronous=True))
    # 5.x: the sync connection opens in __init__, which reads the session's details only after _connect().
    when_imported(f"{connector}.connection._connection", _connection("Connection", "__init__", asynchronous=False))
    when_imported(f"{connector}.cursor._base", _cursor(asynchronous=False))
    when_imported(f"{connector}.aio.connection._connection", _connection("Connection", "connect", asynchronous=True))
    when_imported(f"{connector}.aio.cursor._base", _cursor(asynchronous=True))
