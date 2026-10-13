"""Oracle: python-oracledb, sync and async. Its cursors are Python classes, wrapped in place.

EXPLAIN isn't available: Oracle's plans come from EXPLAIN PLAN FOR and DBMS_XPLAN, which tracepanel doesn't run.
"""

from types import ModuleType
from typing import Any

from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs
from tracepanel.integrations.dbapi import _core


def _attrs(cursor: Any) -> Attrs:  # noqa: ANN401 (an oracledb cursor)
    connection = cursor.connection
    dsn = str(getattr(connection, "dsn", "") or "")
    # host:port/service, the common form; a TNS alias or descriptor names no server here.
    server, _, service = dsn.partition("/")
    host, _, port = server.partition(":") if service else ("", "", "")
    name = service or str(getattr(connection, "service_name", "") or dsn)
    return _core.attrs(_core.Where("oracledb", "oracle", host, port, name))


def _oracledb(oracledb: ModuleType) -> None:
    _core.wrap(oracledb.Cursor, _core.DBAPI, db=_attrs)
    _core.wrap(oracledb.AsyncCursor, _core.DBAPI, db=_attrs, asynchronous=True)


def install() -> None:
    """Wrap python-oracledb once the app imports it."""
    when_imported("oracledb", _oracledb)
