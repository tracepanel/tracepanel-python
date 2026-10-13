"""sqlite3 (the standard library) and aiosqlite on top of it.

sqlite3's classes are C types: connect() gets a Connection subclass whose cursors are traced, and a factory the app
(or Django) passes is subclassed rather than replaced. aiosqlite runs sqlite3 in a thread of its own, where the
request's trace isn't current: its methods are wrapped where the app awaits them.
"""

import contextlib
import functools
import sqlite3
import sqlite3.dbapi2
from collections.abc import Iterator
from types import ModuleType
from typing import Any

from tracepanel.core import sql
from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs
from tracepanel.core.patching import original, patch
from tracepanel.integrations.dbapi import _core

PATH = "_tracepanel_database"
# connect(database, timeout, detect_types, isolation_level, check_same_thread, factory, ...): factory's position.
_FACTORY_POSITION = 5


def _where(connection: object, client: str) -> _core.Where:
    path = str(getattr(connection, PATH, "") or "")
    # A file can be opened again for EXPLAIN; an in-memory database is only in the app's connection.
    explain = None if not path or path.startswith((":memory:", "file:")) or "mode=memory" in path else _explainer(path)
    return _core.Where(client, "sqlite", name=path, explain=explain)


def _explainer(path: str) -> _core.plans.Execute:
    def execute(sql_text: str, values: object) -> tuple[_core.plans.Rows, list[str]]:
        connection = original(sqlite3, "connect")(f"file:{path}?mode=ro", uri=True)
        try:
            return _core.cursor_rows(sqlite3.Cursor(connection), sql_text, values)
        finally:
            connection.close()

    return execute


def _attrs(connection: object, client: str = "sqlite3") -> Attrs:
    return _core.attrs(_where(connection, client))


class _Cursor:
    """Mixed into a sqlite3.Cursor class: its statements are recorded."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (sqlite3's own signature, passed on unchanged)
        query, params, _ = _core.dbapi_execute(args, kwargs)
        run = functools.partial(super().execute, *args, **kwargs)  # type: ignore[misc]
        return _counts(self, _core.run(run, query, params, many=False, db=lambda: _attrs(self.connection), cursor=self))  # type: ignore[attr-defined]

    def executemany(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        query, _, _ = _core.dbapi_executemany(args, kwargs)
        run = functools.partial(super().executemany, *args, **kwargs)  # type: ignore[misc]
        return _counts(self, _core.run(run, query, None, many=True, db=lambda: _attrs(self.connection), cursor=self))  # type: ignore[attr-defined]

    def executescript(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        script, _, _ = _core.dbapi_script(args, kwargs)
        run = functools.partial(super().executescript, *args, **kwargs)  # type: ignore[misc]
        return _counts(self, _core.run(run, script, None, many=False, db=lambda: _attrs(self.connection), cursor=self))  # type: ignore[attr-defined]


class _Counting:
    """Mixed into a traced cursor while its rows count into a SELECT's span: sqlite3 doesn't say how many it found.

    Only then: a cursor whose statement isn't recorded (Django's, SQLAlchemy's: they count their own) fetches as
    sqlite3's.
    """

    def fetchone(self) -> Any:  # noqa: ANN401
        row = super().fetchone()  # type: ignore[misc]
        sql.fetched(self, sql.size(row))
        return row

    def fetchmany(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        rows = super().fetchmany(*args, **kwargs)  # type: ignore[misc]
        sql.fetched(self, len(rows))
        return rows

    def fetchall(self) -> Any:  # noqa: ANN401
        rows = super().fetchall()  # type: ignore[misc]
        sql.fetched(self, len(rows))
        return rows

    def __iter__(self) -> Iterator[Any]:
        # Counted once, when the iteration ends.
        rows = 0
        try:
            while True:
                try:
                    row = sqlite3.Cursor.__next__(self)  # type: ignore[arg-type]
                except StopIteration:
                    return
                rows += 1
                yield row
        finally:
            sql.fetched(self, rows)


def _counts(cursor: Any, result: object) -> object:  # noqa: ANN401 (a traced sqlite3 cursor)
    """The cursor counts what is fetched from it while its statement's span waits for the rows, else not."""
    counting = issubclass(type(cursor), _Counting)
    if (sql.counting(cursor) is not None) != counting:
        with contextlib.suppress(TypeError):  # a class whose layout differs: left as it is
            cursor.__class__ = type(cursor).__bases__[1] if counting else _core.subclass(type(cursor), _Counting)
    return result


class _Connection:
    """Mixed into a sqlite3.Connection class: its cursors, and its execute() shortcuts, are recorded."""

    def cursor(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        factory = kwargs.pop("factory", args[0] if args else sqlite3.Cursor)
        return super().cursor(_core.subclass(factory, _Cursor))  # type: ignore[misc]

    # As sqlite3 documents them: a new cursor, and its method called.
    def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self.cursor().executescript(*args, **kwargs)


def _connect(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (sqlite3.connect's signature)
    positional = list(args)
    factory = kwargs.pop(
        "factory",
        positional.pop(_FACTORY_POSITION) if len(positional) > _FACTORY_POSITION else sqlite3.Connection,
    )
    connection = original(sqlite3, "connect")(*positional, factory=_core.subclass(factory, _Connection), **kwargs)
    path = positional[0] if positional else kwargs.get("database", "")
    # A factory with __slots__ takes no path: the statements are recorded all the same.
    with contextlib.suppress(AttributeError):
        setattr(connection, PATH, str(path.decode() if isinstance(path, bytes) else path))
    return connection


def _aiosqlite_attrs(owner: Any) -> Attrs:  # noqa: ANN401 (an aiosqlite Connection or Cursor)
    connection = getattr(owner, "_conn", None)  # the Cursor's aiosqlite Connection
    connection = getattr(connection, "_connection", None) or getattr(owner, "_connection", None)
    return _attrs(connection, "aiosqlite")


def install() -> None:
    patch(sqlite3, "connect", _connect)
    # Django's backend and others import the driver as sqlite3.dbapi2.
    patch(sqlite3.dbapi2, "connect", _connect)
    when_imported("aiosqlite", _aiosqlite)


def _aiosqlite(aiosqlite: ModuleType) -> None:
    methods = {**_core.DBAPI, "executescript": _core.dbapi_script}
    for cls in (aiosqlite.Connection, aiosqlite.Cursor):
        _core.wrap(cls, methods, db=_aiosqlite_attrs, asynchronous=True)
    _core.count_fetches(aiosqlite.Cursor)
