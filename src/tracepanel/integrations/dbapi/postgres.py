"""PostgreSQL: psycopg (3), psycopg2 and asyncpg.

- psycopg: its cursors are Python classes, wrapped in place, sync and async (server-side cursors too).
- psycopg2: its classes are C types, so connect() gets a connection subclass whose cursors are traced; the app's
  connection_factory and cursor_factory are subclassed rather than replaced.
- asyncpg: not DB-API; its Connection's query methods (which its Pool's call), its prepared statements' and its
  server-side cursors are wrapped.
"""

import asyncio
import functools
import importlib
import weakref
from collections.abc import Awaitable, Callable, Generator
from types import ModuleType
from typing import Any

from tracepanel.core import sql
from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs, Span
from tracepanel.core.patching import original, patch
from tracepanel.integrations.dbapi import _core


def _optional(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _rendered(query: object, owner: Any) -> object:  # noqa: ANN401 (a cursor)
    """A psycopg sql.Composed as the server gets it; a string or bytes as it is."""
    as_string = getattr(query, "as_string", None)
    if as_string is None:
        return query
    try:
        return as_string(owner)
    except Exception:  # noqa: BLE001 (a composition that can't render is still the app's statement)
        return str(query)


def _explainer(connect: Any, params: dict[str, Any]) -> _core.plans.Execute:  # noqa: ANN401 (a driver's connect)
    def execute(sql_text: str, values: object, session: _core.plans.Session) -> tuple[_core.plans.Rows, list[str]]:
        connection = connect(**params)
        try:
            return _core.cursor_rows(connection.cursor(), sql_text, values, session)
        finally:
            connection.close()

    return execute


# ---------- psycopg (3) ----------


def _psycopg_attrs(cursor: Any) -> Attrs:  # noqa: ANN401 (a psycopg cursor)
    return _psycopg_connection_attrs(cursor.connection)


def _psycopg_connection_attrs(connection: Any) -> Attrs:  # noqa: ANN401 (a psycopg connection)
    info = connection.info
    import psycopg  # noqa: PLC0415 (installed: its cursor is here)

    params = {
        "host": info.host,
        "port": info.port,
        "dbname": info.dbname,
        "user": info.user,
        "password": info.password,
    }
    return _core.attrs(
        _core.Where(
            "psycopg",
            "postgresql",
            str(info.host or ""),
            str(info.port or ""),
            str(info.dbname or ""),
            _explainer(psycopg.connect, params),
        )
    )


def _fetching(method: Callable[..., Generator[Any, Any, Any]]) -> Callable[..., Generator[Any, Any, Any]]:
    """A page of a psycopg server-side cursor's rows, counted into the span of the statement it ran."""

    @functools.wraps(method)
    def counted(self: Any, *args: Any) -> Generator[Any, Any, Any]:  # noqa: ANN401
        rows = yield from method(self, *args)
        sql.fetched(self, len(rows))
        return rows

    return counted


def _psycopg_connect(cls: type, *, asynchronous: bool) -> None:
    """Connection.connect() and AsyncConnection.connect(), the classmethods that open them (psycopg_pool's too)."""
    method = vars(cls)["connect"].__func__
    where = _psycopg_connection_attrs

    def connect(owner: type, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (psycopg's own signature)
        return _core.opening(lambda: method(owner, *args, **kwargs), client="psycopg", system="postgresql", where=where)

    async def connect_async(owner: type, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return await _core.opening_async(
            lambda: method(owner, *args, **kwargs), client="psycopg", system="postgresql", where=where
        )

    patch(cls, "connect", classmethod(functools.wraps(method)(connect_async if asynchronous else connect)))


def _install_psycopg(psycopg: ModuleType) -> None:
    _psycopg_connect(psycopg.Connection, asynchronous=False)
    _psycopg_connect(psycopg.AsyncConnection, asynchronous=True)
    # psycopg.connect is Connection.connect, bound when psycopg was imported.
    patch(psycopg, "connect", psycopg.Connection.connect)
    # Cursor and ServerCursor define execute(); the other cursor classes inherit one of them (likewise async).
    for name in ("Cursor", "ServerCursor"):
        _core.wrap(getattr(psycopg, name), _core.DBAPI, db=_psycopg_attrs, statement=_rendered)
    for name in ("AsyncCursor", "AsyncServerCursor"):
        _core.wrap(getattr(psycopg, name), _core.DBAPI, db=_psycopg_attrs, statement=_rendered, asynchronous=True)
    # A server-side cursor reads its rows a page at a time, sync or async, through the mixin's _fetch_gen.
    mixin = next((cls for cls in psycopg.ServerCursor.__mro__ if "_fetch_gen" in vars(cls)), None)
    if mixin is not None:
        patch(mixin, "_fetch_gen", _fetching(vars(mixin)["_fetch_gen"]))


# ---------- psycopg2 ----------


def _psycopg2_attrs(connection: Any) -> Attrs:  # noqa: ANN401 (a psycopg2 connection)
    info = connection.info
    import psycopg2  # noqa: PLC0415

    params = {
        "host": info.host,
        "port": info.port,
        "dbname": info.dbname,
        "user": info.user,
        "password": info.password,
    }
    return _core.attrs(
        _core.Where(
            "psycopg2",
            "postgresql",
            str(info.host or ""),
            str(info.port or ""),
            str(info.dbname or ""),
            _explainer(original(psycopg2, "connect"), params),
        )
    )


class _Psycopg2Cursor:
    """Mixed into a psycopg2 cursor class (extensions.cursor, extras.DictCursor…): its statements are recorded."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (psycopg2's own signature)
        query, params, _ = _core.dbapi_execute(args, {**kwargs, "params": kwargs.get("vars")})
        return _core.run(
            lambda: super(_Psycopg2Cursor, self).execute(*args, **kwargs),  # type: ignore[misc]
            _rendered(query, self),
            params,
            many=False,
            db=lambda: _psycopg2_attrs(self.connection),  # type: ignore[attr-defined]
        )

    def executemany(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        query, _, _ = _core.dbapi_executemany(args, kwargs)
        return _core.run(
            lambda: super(_Psycopg2Cursor, self).executemany(*args, **kwargs),  # type: ignore[misc]
            _rendered(query, self),
            None,
            many=True,
            db=lambda: _psycopg2_attrs(self.connection),  # type: ignore[attr-defined]
        )


class _Psycopg2Connection:
    """Mixed into a psycopg2 connection class: its cursors are traced, whatever factory makes them."""

    def cursor(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (name, cursor_factory, withhold, scrollable)
        import psycopg2.extensions  # noqa: PLC0415

        positional = list(args)
        factory = kwargs.pop("cursor_factory", positional.pop(1) if len(positional) > 1 else None)
        factory = factory or self.cursor_factory or psycopg2.extensions.cursor  # type: ignore[attr-defined]
        kwargs["cursor_factory"] = _core.subclass(factory, _Psycopg2Cursor)
        return super().cursor(*positional, **kwargs)  # type: ignore[misc]


def _psycopg2_connect(
    dsn: str | None = None, connection_factory: type | None = None, cursor_factory: type | None = None, **kwargs: object
) -> Any:  # noqa: ANN401 (a psycopg2 connection)
    import psycopg2.extensions  # noqa: PLC0415

    factory = _core.subclass(connection_factory or psycopg2.extensions.connection, _Psycopg2Connection)
    return _core.opening(
        lambda: original(psycopg2, "connect")(dsn, connection_factory=factory, cursor_factory=cursor_factory, **kwargs),
        client="psycopg2",
        system="postgresql",
        where=_psycopg2_attrs,
    )


def _install_psycopg2(psycopg2: ModuleType) -> None:
    patch(psycopg2, "connect", _psycopg2_connect)


# ---------- asyncpg ----------


def _positional(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """fetch(query, *args), fetchrow, fetchval, execute: the parameters follow the query."""
    return (args[0] if args else kwargs.get("query", "")), tuple(args[1:]) or None, False


def _many(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """executemany(command, args), fetchmany(query, args): one statement, many rows of parameters."""
    return (args[0] if args else kwargs.get("query", kwargs.get("command", ""))), None, True


_ASYNCPG: dict[str, _core.Reads] = {
    "execute": _positional,
    "fetch": _positional,
    "fetchrow": _positional,
    "fetchval": _positional,
    "executemany": _many,
    "fetchmany": _many,
}


def _asyncpg_explainer(params: dict[str, Any]) -> _core.plans.Execute:
    import asyncpg  # noqa: PLC0415

    # PostgreSQL's EXPLAIN takes no session around it.
    def execute(sql_text: str, values: object, _session: _core.plans.Session) -> tuple[_core.plans.Rows, list[str]]:
        async def explain() -> tuple[_core.plans.Rows, list[str]]:
            connection = await asyncpg.connect(**params)
            try:
                args = list(values.values()) if isinstance(values, dict) else list(_core.params_of(values) or ())
                records = await connection.fetch(sql_text, *args)
                columns = list(records[0].keys()) if records else ["QUERY PLAN"]
                return [tuple(record.values()) for record in records], columns
            finally:
                await connection.close()

        # The console's thread has no event loop of its own.
        return asyncio.run(explain())

    return execute


def _asyncpg_attrs(owner: Any) -> Attrs:  # noqa: ANN401 (an asyncpg Connection, or a Pool)
    connection = owner
    params = getattr(connection, "_params", None) or getattr(owner, "_working_params", None)
    address = getattr(connection, "_addr", None) or getattr(owner, "_working_addr", None)
    host, port = (address if isinstance(address, tuple) else (address or "", ""))[:2]
    name = str(getattr(params, "database", "") or "")
    explain = None
    if params is not None:
        explain = _asyncpg_explainer(
            {
                "host": host or None,
                "port": port or None,
                "user": params.user,
                "password": params.password,
                "database": name,
            }
        )
    return _core.attrs(_core.Where("asyncpg", "postgresql", str(host), str(port), name, explain))


# A prepared statement's methods: its query is its own, the arguments are the parameters.
def _prepared_positional(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """fetch(*args), fetchrow, fetchval."""
    del kwargs
    return None, tuple(args) or None, False


def _prepared_many(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[object, sql.Params, bool]:
    """fetchmany(args), executemany(args): many rows of parameters."""
    del args, kwargs
    return None, None, True


_PREPARED: dict[str, _core.Reads] = {
    "fetch": _prepared_positional,
    "fetchrow": _prepared_positional,
    "fetchval": _prepared_positional,
    "fetchmany": _prepared_many,
    "executemany": _prepared_many,
}

# A server-side cursor (a portal) is recorded when it is opened, and the rows read from it later count into that span.
# asyncpg's cursors take no attributes: the span waits by connection and portal, the newest few per connection.
_PORTALS: weakref.WeakKeyDictionary[Any, dict[str, Span]] = weakref.WeakKeyDictionary()
_PORTALS_KEPT = 16


def _portal_opened(cursor: Any, span: Span) -> None:  # noqa: ANN401 (an asyncpg cursor)
    portals = _PORTALS.setdefault(cursor._connection, {})  # noqa: SLF001
    portals[cursor._portal_name] = span  # noqa: SLF001
    while len(portals) > _PORTALS_KEPT:
        del portals[next(iter(portals))]


def _portal_span(cursor: Any) -> Span | None:  # noqa: ANN401
    return _PORTALS.get(cursor._connection, {}).get(cursor._portal_name)  # noqa: SLF001


def _cursor_opening(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """_bind_exec (an iterated cursor: opened with its first rows) and _bind (an awaited one): the statement."""

    @functools.wraps(method)
    async def traced(self: Any, *args: Any) -> Any:  # noqa: ANN401
        def opened(span: Span, rows: object) -> None:
            _portal_opened(self, span)
            if isinstance(rows, list):
                sql.count_rows(span, len(rows))

        return await _core.run_async(
            lambda: method(self, *args),
            self._query,
            tuple(self._args) or None,
            many=False,
            db=lambda: _asyncpg_attrs(self._connection),
            then=opened,
        )

    return traced


def _cursor_reading(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """_exec: more rows from an open portal, counted into the span of the statement that opened it."""

    @functools.wraps(method)
    async def counted(self: Any, *args: Any) -> Any:  # noqa: ANN401
        rows = await method(self, *args)
        span = _portal_span(self)
        if span is not None and isinstance(rows, list):
            sql.count_rows(span, len(rows))
        return rows

    return counted


def _cursor_closing(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(method)
    async def closing(self: Any, *args: Any) -> Any:  # noqa: ANN401
        _PORTALS.get(self._connection, {}).pop(self._portal_name, None)
        return await method(self, *args)

    return closing


def _asyncpg_connect(connect: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """connect_utils._connect: what asyncpg.connect() and its pool's new connections go through."""

    @functools.wraps(connect)
    async def traced(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (asyncpg's own signature)
        return await _core.opening_async(
            lambda: connect(*args, **kwargs), client="asyncpg", system="postgresql", where=_asyncpg_attrs
        )

    return traced


def _install_asyncpg(asyncpg: ModuleType) -> None:
    utils = asyncpg.connect_utils
    patch(utils, "_connect", _asyncpg_connect(utils._connect))  # noqa: SLF001
    # The pool's methods take a connection and call its own: the connection's wrapper records the statement.
    _core.wrap(asyncpg.connection.Connection, _ASYNCPG, db=_asyncpg_attrs, asynchronous=True)
    _core.wrap(
        asyncpg.prepared_stmt.PreparedStatement,
        _PREPARED,
        db=lambda statement: _asyncpg_attrs(statement._connection),  # noqa: SLF001
        statement=lambda _query, statement: statement._query,  # noqa: SLF001
        asynchronous=True,
    )
    cursor = asyncpg.cursor.BaseCursor
    patch(cursor, "_bind_exec", _cursor_opening(cursor._bind_exec))  # noqa: SLF001
    patch(cursor, "_bind", _cursor_opening(cursor._bind))  # noqa: SLF001
    patch(cursor, "_exec", _cursor_reading(cursor._exec))  # noqa: SLF001
    patch(cursor, "_close_portal", _cursor_closing(cursor._close_portal))  # noqa: SLF001


def install() -> None:
    """Wrap each driver once the app imports it."""
    when_imported("psycopg", _install_psycopg)
    when_imported("psycopg2", _install_psycopg2)
    when_imported("asyncpg", _install_asyncpg)
