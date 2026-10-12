"""PostgreSQL: psycopg (3), psycopg2 and asyncpg.

- psycopg: its cursors are Python classes, wrapped in place, sync and async (server-side cursors too).
- psycopg2: its classes are C types, so connect() gets a connection subclass whose cursors are traced; the app's
  connection_factory and cursor_factory are subclassed rather than replaced.
- asyncpg: not DB-API; its Connection's and Pool's query methods are wrapped.
"""

import asyncio
import importlib
from types import ModuleType
from typing import Any

from tracepanel.core import sql
from tracepanel.core.model import Attrs
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
    def execute(sql_text: str, values: object) -> tuple[_core.plans.Rows, list[str]]:
        connection = connect(**params)
        try:
            return _core.cursor_rows(connection.cursor(), sql_text, values)
        finally:
            connection.close()

    return execute


# ---------- psycopg (3) ----------


def _psycopg_attrs(cursor: Any) -> Attrs:  # noqa: ANN401 (a psycopg cursor)
    info = cursor.connection.info
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


def _install_psycopg() -> None:
    psycopg = _optional("psycopg")
    if psycopg is None:
        return
    # Cursor and ServerCursor define execute(); the other cursor classes inherit one of them (likewise async).
    for name in ("Cursor", "ServerCursor"):
        _core.wrap(getattr(psycopg, name), _core.DBAPI, db=_psycopg_attrs, statement=_rendered)
    for name in ("AsyncCursor", "AsyncServerCursor"):
        _core.wrap(getattr(psycopg, name), _core.DBAPI, db=_psycopg_attrs, statement=_rendered, asynchronous=True)


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
    return original(psycopg2, "connect")(dsn, connection_factory=factory, cursor_factory=cursor_factory, **kwargs)


def _install_psycopg2() -> None:
    psycopg2 = _optional("psycopg2")
    if psycopg2 is not None:
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

    def execute(sql_text: str, values: object) -> tuple[_core.plans.Rows, list[str]]:
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


def _install_asyncpg() -> None:
    asyncpg = _optional("asyncpg")
    if asyncpg is None:
        return
    # The pool's methods take a connection and call its own: the connection's wrapper records the statement.
    _core.wrap(asyncpg.connection.Connection, _ASYNCPG, db=_asyncpg_attrs, asynchronous=True)


def install() -> None:
    _install_psycopg()
    _install_psycopg2()
    _install_asyncpg()
