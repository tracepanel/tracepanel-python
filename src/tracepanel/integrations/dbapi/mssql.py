"""SQL Server: pyodbc and pymssql.

Their connections and cursors are C (pyodbc) and Cython (pymssql) types: none can be patched, and the drivers make
their cursors themselves. connect() hands the app its connection in a thin wrapper whose cursors record their
statements; everything else is the driver's own. A connection Django's backend (mssql-django) or SQLAlchemy opens is
left as it is: they record their statements themselves. EXPLAIN opens a new connection with the same arguments.
"""

import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import ModuleType
from typing import Any, Self

from tracepanel.core import sql
from tracepanel.core.imports import when_imported
from tracepanel.core.patching import original, patch
from tracepanel.integrations.dbapi import _core

# The wrappers' own attributes; any other is the driver's (pyodbc's fast_executemany, a connection's autocommit).
_OWN = frozenset({"_tracepanel_raw", "_tracepanel_where", "_tracepanel_rows_span"})
# pymssql.connect's parameters, in order.
_PYMSSQL_ARGS = (
    "server",
    "user",
    "password",
    "database",
    "timeout",
    "login_timeout",
    "charset",
    "as_dict",
    "host",
    "appname",
    "port",
)
# pyodbc.connect's own keywords; any other joins the connection string (`host` as SERVER).
_PYODBC_OPTIONS = frozenset({"autocommit", "ansi", "timeout", "readonly", "attrs_before", "encoding"})
_PAIR = re.compile(r"\s*([^=;]+?)\s*=\s*(\{(?:[^}]|\}\})*\}|[^;]*)")


def _values(args: tuple[Any, ...], kwargs: dict[str, Any]) -> sql.Params:
    """execute()'s parameters: a sequence or a mapping, a single value, or pyodbc's values one by one."""
    if len(args) > 2:  # noqa: PLR2004 (the statement and two values or more)
        return tuple(args[1:])
    values = args[1] if len(args) == 2 else kwargs.get("params", kwargs.get("parameters"))  # noqa: PLR2004
    if values is None or isinstance(values, Mapping) or (isinstance(values, Sequence) and not isinstance(values, str)):
        return _core.params_of(values)
    return (values,)


def _statement(args: tuple[Any, ...], kwargs: dict[str, Any]) -> object:
    return args[0] if args else kwargs.get("operation", kwargs.get("sql", ""))


class _Cursor:
    """A driver's cursor whose statements are recorded, and whose fetched rows count into their span."""

    __slots__ = ("__weakref__", "_tracepanel_raw", "_tracepanel_rows_span", "_tracepanel_where")

    def __init__(self, raw: Any, where: _core.Where) -> None:  # noqa: ANN401 (the driver's cursor)
        object.__setattr__(self, "_tracepanel_raw", raw)
        object.__setattr__(self, "_tracepanel_where", where)
        object.__setattr__(self, "_tracepanel_rows_span", None)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._tracepanel_raw, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in _OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._tracepanel_raw, name, value)

    def _run(self, method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], *, many: bool) -> Any:  # noqa: ANN401
        result = _core.run(
            lambda: method(*args, **kwargs),
            _statement(args, kwargs),
            None if many else _values(args, kwargs),
            many=many,
            db=lambda: _core.attrs(self._tracepanel_where),
            cursor=self,
        )
        # pyodbc's execute returns its cursor, for `cursor.execute(...).fetchall()`: this one, then.
        return self if result is self._tracepanel_raw else result

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (the driver's own signature)
        return self._run(self._tracepanel_raw.execute, args, kwargs, many=False)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self._run(self._tracepanel_raw.executemany, args, kwargs, many=True)

    def fetchone(self) -> Any:  # noqa: ANN401
        row = self._tracepanel_raw.fetchone()
        sql.fetched(self, sql.size(row))
        return row

    def fetchmany(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        rows = self._tracepanel_raw.fetchmany(*args, **kwargs)
        sql.fetched(self, len(rows))
        return rows

    def fetchall(self) -> Any:  # noqa: ANN401
        rows = self._tracepanel_raw.fetchall()
        sql.fetched(self, len(rows))
        return rows

    def fetchval(self) -> Any:  # noqa: ANN401 (pyodbc's: the first column of the first row)
        value = self._tracepanel_raw.fetchval()
        sql.fetched(self, 1)
        return value

    def __iter__(self) -> Iterator[Any]:
        # Counted once, when the iteration ends.
        rows = 0
        try:
            for row in self._tracepanel_raw:
                rows += 1
                yield row
        finally:
            sql.fetched(self, rows)

    def __next__(self) -> Any:  # noqa: ANN401
        row = next(self._tracepanel_raw)
        sql.fetched(self, 1)
        return row

    def __enter__(self) -> Self:
        self._tracepanel_raw.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> object:
        return self._tracepanel_raw.__exit__(*exc_info)


class _Connection:
    """A driver's connection whose cursors record their statements."""

    __slots__ = ("__weakref__", "_tracepanel_raw", "_tracepanel_where")

    def __init__(self, raw: Any, where: _core.Where) -> None:  # noqa: ANN401 (the driver's connection)
        object.__setattr__(self, "_tracepanel_raw", raw)
        object.__setattr__(self, "_tracepanel_where", where)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._tracepanel_raw, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in _OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._tracepanel_raw, name, value)

    def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:  # noqa: ANN401
        return _Cursor(self._tracepanel_raw.cursor(*args, **kwargs), self._tracepanel_where)

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (pyodbc's shortcut: a new cursor's execute)
        return self.cursor().execute(*args, **kwargs)

    def __enter__(self) -> Self:
        self._tracepanel_raw.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> object:
        return self._tracepanel_raw.__exit__(*exc_info)


def _server(value: str) -> tuple[str, str]:
    r"""(host, port) of a SQL Server address: `tcp:db,1433`, `db:1433`, `db\\INSTANCE`."""
    value = value.strip().removeprefix("tcp:")
    host, comma, port = value.partition(",")
    if not comma:
        host, colon, port = value.partition(":")
        port = port if colon else ""
    return host.strip(), port.strip()


def _explainer(connect: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> _core.plans.Execute:
    def execute(sql_text: str, values: object, session: _core.plans.Session) -> tuple[_core.plans.Rows, list[str]]:
        connection = connect(*args, **kwargs)
        try:
            return _core.cursor_rows(connection.cursor(), sql_text, values, session)
        finally:
            connection.close()

    return execute


def _wrapped(connect: Callable[..., Any], where: Callable[[], _core.Where]) -> Any:  # noqa: ANN401 (a connection)
    """The connection, wrapped when the app opens it; Django's backend and SQLAlchemy get the driver's own."""
    connection = connect()
    # The first frame above this package's own (the connect() wrapper's) is who opened it.
    if not _core.CALLERS.app_called(sys._getframe(1)):  # noqa: SLF001 (the stack tells who called)
        return connection
    return _Connection(connection, where())


# ---------- pyodbc ----------


def _pyodbc_where(args: tuple[Any, ...], kwargs: dict[str, Any]) -> _core.Where:
    text = str(args[0] if args else kwargs.get("connstring", ""))
    pairs = {key.lower(): value.strip("{}") for key, value in _PAIR.findall(text)}
    pairs.update({key.lower(): str(value) for key, value in kwargs.items() if key not in _PYODBC_OPTIONS})
    host, port = _server(pairs.get("server") or pairs.get("host") or pairs.get("address") or pairs.get("addr") or "")
    import pyodbc  # noqa: PLC0415 (installed: it is connecting)

    explain = _explainer(original(pyodbc, "connect"), args, kwargs)
    return _core.Where("pyodbc", "mssql", host, port, pairs.get("database", ""), explain)


def _pyodbc_connect(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (pyodbc.connect's signature)
    import pyodbc  # noqa: PLC0415

    return _wrapped(lambda: original(pyodbc, "connect")(*args, **kwargs), lambda: _pyodbc_where(args, kwargs))


# ---------- pymssql ----------


def _pymssql_where(args: tuple[Any, ...], kwargs: dict[str, Any]) -> _core.Where:
    named = {**dict(zip(_PYMSSQL_ARGS, args, strict=False)), **kwargs}
    host, port = _server(str(named.get("host") or named.get("server") or ""))
    host = "" if host == "." else host
    import pymssql  # noqa: PLC0415

    # Rows as tuples for EXPLAIN, whatever the app's connection returns.
    explain = _explainer(original(pymssql, "connect"), (), {**named, "as_dict": False})
    return _core.Where(
        "pymssql", "mssql", host, str(named.get("port") or port), str(named.get("database") or ""), explain
    )


def _pymssql_connect(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (pymssql.connect's signature)
    import pymssql  # noqa: PLC0415

    return _wrapped(lambda: original(pymssql, "connect")(*args, **kwargs), lambda: _pymssql_where(args, kwargs))


def _pyodbc(pyodbc: ModuleType) -> None:
    patch(pyodbc, "connect", _pyodbc_connect)


def _pymssql(pymssql: ModuleType) -> None:
    patch(pymssql, "connect", _pymssql_connect)


def install() -> None:
    """Wrap each driver's connect() once the app imports it."""
    when_imported("pyodbc", _pyodbc)
    when_imported("pymssql", _pymssql)
