"""MySQL and MariaDB: PyMySQL, mysqlclient, MySQL Connector/Python (sync and asyncio), aiomysql and asyncmy.

Their cursors are Python classes: execute() and executemany() are wrapped where each is defined. asyncmy's cursors are
Cython types: a connection's cursors are made of a traced subclass of the class asked for. EXPLAIN opens a new
connection with the same driver (aiomysql's and asyncmy's through PyMySQL, the protocol they share).
"""

import importlib
from collections.abc import Callable
from contextvars import ContextVar
from types import ModuleType
from typing import Any

from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs
from tracepanel.core.patching import original, patch
from tracepanel.integrations.dbapi import _core

# Where mysqlclient's connections were opened to: its C connection keeps no host or database to read back.
_OPENED = "_tracepanel_opened"


def _optional(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _explainer(connect: Any, params: dict[str, Any]) -> _core.plans.Execute:  # noqa: ANN401 (a driver's connect)
    def execute(sql_text: str, values: object) -> tuple[_core.plans.Rows, list[str]]:
        connection = connect(**params)
        try:
            return _core.cursor_rows(connection.cursor(), sql_text, values)
        finally:
            connection.close()

    return execute


def _where(client: str, host: object, port: object, name: object, explain: _core.plans.Execute | None) -> Attrs:
    return _core.attrs(_core.Where(client, "mysql", _text(host), _text(port), _text(name), explain))


# ---------- PyMySQL and aiomysql ----------


def _pymysql_params(connection: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "host": connection.host,
        "port": connection.port,
        "user": _text(getattr(connection, "user", "")),
        "password": getattr(connection, "password", None) or getattr(connection, "_password", ""),
        "database": _text(connection.db) or None,
    }


def _pymysql_attrs(cursor: Any) -> Attrs:  # noqa: ANN401
    import pymysql  # noqa: PLC0415 (installed: its cursor is here)

    connection = cursor.connection
    return _where(
        "pymysql",
        connection.host,
        connection.port,
        connection.db,
        _explainer(pymysql.connect, _pymysql_params(connection)),
    )


def _aiomysql_attrs(cursor: Any) -> Attrs:  # noqa: ANN401
    connection = cursor.connection
    pymysql = _optional("pymysql")
    explain = _explainer(pymysql.connect, _pymysql_params(connection)) if pymysql else None
    return _where("aiomysql", connection.host, connection.port, connection.db, explain)


# ---------- mysqlclient ----------


def _mysqlclient_init(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 (MySQLdb's Connection.__init__)
    import MySQLdb.connections  # noqa: PLC0415

    original(MySQLdb.connections.Connection, "__init__")(self, *args, **kwargs)
    setattr(self, _OPENED, dict(kwargs))


def _mysqlclient_attrs(cursor: Any) -> Attrs:  # noqa: ANN401
    import MySQLdb  # noqa: PLC0415

    opened: dict[str, Any] = getattr(cursor.connection, _OPENED, None) or {}
    name = opened.get("database", opened.get("db", ""))
    explain = _explainer(MySQLdb.connect, opened) if opened else None
    return _where("mysqlclient", opened.get("host", ""), opened.get("port", ""), name, explain)


# ---------- MySQL Connector/Python ----------


def _connector_attrs(cursor: Any) -> Attrs:  # noqa: ANN401 (a mysql.connector cursor, sync or asyncio)
    import mysql.connector  # noqa: PLC0415

    connection = cursor._connection  # noqa: SLF001 (the cursor's only reference to its connection)
    host, port, name = connection._host, connection._port, connection._database  # noqa: SLF001
    params = {
        "host": host,
        "port": port,
        "user": connection._user,  # noqa: SLF001
        "password": connection._password,  # noqa: SLF001
        "database": name or None,
    }
    return _where("mysql-connector", host, port, name, _explainer(mysql.connector.connect, params))


def _pymysql(pymysql: ModuleType) -> None:
    _core.wrap(pymysql.cursors.Cursor, _core.DBAPI, db=_pymysql_attrs)


def _aiomysql(aiomysql: ModuleType) -> None:
    _core.wrap(aiomysql.cursors.Cursor, _core.DBAPI, db=_aiomysql_attrs, asynchronous=True)


def _mysqlclient(connections: ModuleType) -> None:
    patch(connections.Connection, "__init__", _mysqlclient_init)
    _core.wrap(importlib.import_module("MySQLdb.cursors").BaseCursor, _core.DBAPI, db=_mysqlclient_attrs)


def _connector(names: tuple[str, ...], *, asynchronous: bool = False) -> Callable[[ModuleType], None]:
    def wrap(module: ModuleType) -> None:
        for name in names:
            _core.wrap(getattr(module, name), _core.DBAPI, db=_connector_attrs, asynchronous=asynchronous)

    return wrap


# ---------- asyncmy ----------


def _asyncmy_attrs(cursor: Any) -> Attrs:  # noqa: ANN401
    connection = cursor.connection
    pymysql = _optional("pymysql")
    params = {
        "host": connection._host,  # noqa: SLF001 (asyncmy keeps them private, with no property)
        "port": connection._port,  # noqa: SLF001
        "user": _text(connection._user),  # noqa: SLF001
        "password": connection._password,  # noqa: SLF001
        "database": _text(connection._db) or None,  # noqa: SLF001
    }
    explain = _explainer(pymysql.connect, params) if pymysql else None
    return _where("asyncmy", params["host"], params["port"], params["database"], explain)


# Set while asyncmy's executemany runs: it calls execute once per row, which is that one statement, not more.
_many: ContextVar[bool] = ContextVar("tracepanel_asyncmy_many", default=False)


class _AsyncmyCursor:
    """Mixed into an asyncmy cursor class: its statements are recorded."""

    async def execute(self, query: object, args: object = None) -> Any:  # noqa: ANN401 (the rows it affected)
        if _many.get():
            return await super().execute(query, args)  # type: ignore[misc]
        return await _core.run_async(
            lambda: super(_AsyncmyCursor, self).execute(query, args),  # type: ignore[misc]
            query,
            _core.params_of(args),
            many=False,
            db=lambda: _asyncmy_attrs(self),
        )

    async def executemany(self, query: object, args: object) -> Any:  # noqa: ANN401
        token = _many.set(True)
        try:
            return await _core.run_async(
                lambda: super(_AsyncmyCursor, self).executemany(query, args),  # type: ignore[misc]
                query,
                None,
                many=True,
                db=lambda: _asyncmy_attrs(self),
            )
        finally:
            _many.reset(token)


def _asyncmy(connection_module: ModuleType) -> None:
    connection = connection_module.Connection
    cursor = vars(connection)["cursor"]

    def traced(self: Any, cursor_cls: type | None = None) -> Any:  # noqa: ANN401 (an asyncmy cursor)
        # The class asked for, else the connection's own (connect's cursor_cls), with the recording in front of it.
        return cursor(self, _core.subclass(cursor_cls or self._cursor_cls, _AsyncmyCursor))

    patch(connection, "cursor", traced)


def install() -> None:
    """Wrap each driver once the app imports it."""
    when_imported("pymysql", _pymysql)
    when_imported("aiomysql", _aiomysql)
    when_imported("asyncmy.connection", _asyncmy)
    when_imported("MySQLdb.connections", _mysqlclient)
    when_imported("mysql.connector.cursor", _connector(("MySQLCursor", "MySQLCursorPrepared")))
    when_imported("mysql.connector.cursor_cext", _connector(("CMySQLCursor", "CMySQLCursorPrepared")))
    when_imported("mysql.connector.aio.cursor", _connector(("MySQLCursor", "MySQLCursorPrepared"), asynchronous=True))
