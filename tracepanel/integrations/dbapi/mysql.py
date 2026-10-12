"""MySQL and MariaDB: PyMySQL, mysqlclient (MySQLdb), MySQL Connector/Python (sync and asyncio) and aiomysql.

Their cursors are Python classes: execute() and executemany() are wrapped where each is defined. EXPLAIN opens a new
connection with the same driver (aiomysql's through PyMySQL, which it is built on).
"""

import importlib
from types import ModuleType
from typing import Any

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


def install() -> None:
    if (pymysql := _optional("pymysql")) is not None:
        _core.wrap(pymysql.cursors.Cursor, _core.DBAPI, db=_pymysql_attrs)
    if (aiomysql := _optional("aiomysql")) is not None:
        _core.wrap(aiomysql.cursors.Cursor, _core.DBAPI, db=_aiomysql_attrs, asynchronous=True)
    if (mysqldb := _optional("MySQLdb.connections")) is not None:
        patch(mysqldb.Connection, "__init__", _mysqlclient_init)
        _core.wrap(importlib.import_module("MySQLdb.cursors").BaseCursor, _core.DBAPI, db=_mysqlclient_attrs)
    if (connector := _optional("mysql.connector.cursor")) is not None:
        for name in ("MySQLCursor", "MySQLCursorPrepared"):
            _core.wrap(getattr(connector, name), _core.DBAPI, db=_connector_attrs)
    if (cext := _optional("mysql.connector.cursor_cext")) is not None:
        for name in ("CMySQLCursor", "CMySQLCursorPrepared"):
            _core.wrap(getattr(cext, name), _core.DBAPI, db=_connector_attrs)
    if (aio := _optional("mysql.connector.aio.cursor")) is not None:
        for name in ("MySQLCursor", "MySQLCursorPrepared"):
            _core.wrap(getattr(aio, name), _core.DBAPI, db=_connector_attrs, asynchronous=True)
