import asyncio
import sqlite3
from pathlib import Path
from types import FrameType

import pytest
from dirty_equals import IsPartialDict, IsStr

from tests.integrations.dbapi.conftest import sql_spans, statements, traced
from tracepanel.core.explain import ExplainError
from tracepanel.core.model import Trace
from tracepanel.integrations import dbapi


def shop(path: str = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER)")
    connection.executemany("INSERT INTO author (id, name) VALUES (?, ?)", [(1, "a"), (2, "b")])
    connection.executemany("INSERT INTO book (title, author_id) VALUES (?, ?)", [("x", 1), ("y", 2), ("z", 1)])
    connection.commit()
    return connection


def test_statements_are_recorded_once_each() -> None:
    connection = shop()

    def work() -> None:
        books = connection.execute("SELECT title, author_id FROM book").fetchall()
        for _, author_id in books:
            connection.cursor().execute("SELECT name FROM author WHERE id = ?", (author_id,)).fetchone()

    trace = traced(work)

    assert sql_spans(trace) == [("sqlite3", "SELECT book")] + [("sqlite3", "SELECT author")] * 3
    assert statements(trace)[1].attrs == IsPartialDict(
        {
            "db.client": "sqlite3",
            "db.alias": "sqlite3:sqlite",
            "db.vendor": "sqlite",
            "db.system": "sqlite",
            "db.statement": "SELECT name FROM author WHERE id = ?",
            "db.fingerprint": "SELECT name FROM author WHERE id = ?",
            "db.params": [1],
            "db.many": False,
            "code.origin": IsStr(regex=r"tests/integrations/dbapi/test_sqlite\.py:\d+ in work"),
        }
    )


def outside_the_project(frame: FrameType) -> bool:
    del frame
    return False


def test_a_query_no_project_code_made_names_the_library_code_that_did(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = shop()
    # As in Django's admin: no frame of the project's own on the stack.
    monkeypatch.setattr("tracepanel.core.frames._code_in_app", outside_the_project)

    def work() -> None:
        connection.execute("SELECT name FROM author").fetchall()

    (span,) = statements(traced(work))

    assert span.attrs["code.origin"] is None
    assert span.attrs["code.caller"] == IsStr(regex=r".*test_sqlite\.py:\d+ in .*work")


def test_a_query_project_code_made_has_no_library_caller() -> None:
    connection = shop()

    (span,) = statements(traced(lambda: connection.execute("SELECT name FROM author").fetchall()))

    assert span.attrs["code.origin"] == IsStr(regex=r"tests/integrations/dbapi/test_sqlite\.py:\d+ in <lambda>")
    assert "code.caller" not in span.attrs


def test_parameters_tell_repeats_from_a_loop() -> None:
    connection = shop()

    def work() -> None:
        for author_id in (1, 1, 2):
            connection.execute("SELECT name FROM author WHERE id = ?", (author_id,))

    hashes = [span.attrs["db.params_hash"] for span in statements(traced(work))]

    assert hashes[0] == hashes[1] != hashes[2]


def test_named_parameters() -> None:
    connection = shop()

    trace = traced(lambda: connection.execute("SELECT name FROM author WHERE id = :id", {"id": 2}))

    assert statements(trace)[0].attrs == IsPartialDict({"db.params": [2], "db.param_names": ["id"]})


def test_executemany_and_executescript_are_one_statement_each() -> None:
    connection = shop()

    def work() -> None:
        connection.executemany("INSERT INTO author (name) VALUES (?)", [("c",), ("d",)])
        connection.executescript("DELETE FROM book; DELETE FROM author;")

    trace = traced(work)

    assert sql_spans(trace) == [("sqlite3", "INSERT author"), ("sqlite3", "DELETE book")]
    assert [span.attrs["db.many"] for span in statements(trace)] == [True, False]


def test_a_file_names_the_database_and_can_be_explained(tmp_path: Path) -> None:
    connection = shop(str(tmp_path / "shop.sqlite3"))

    trace = traced(lambda: connection.execute("SELECT title FROM book WHERE author_id = ?", (1,)))
    attrs = statements(trace)[0].attrs
    plan = dbapi.explain("SELECT title FROM book WHERE author_id = ?", [1], str(attrs["db.alias"]))

    assert attrs == IsPartialDict({"db.alias": "sqlite3:shop.sqlite3", "db.namespace": "shop.sqlite3"})
    assert plan.to_dict() == IsPartialDict(vendor="sqlite", alias="sqlite3:shop.sqlite3")


def test_an_in_memory_database_cannot_be_explained() -> None:
    connection = shop()
    traced(lambda: connection.execute("SELECT 1"))

    with pytest.raises(ExplainError, match="isn't available for queries made through sqlite3"):
        dbapi.explain("SELECT 1", [], "sqlite3:sqlite")


def test_the_app_s_factories_are_kept() -> None:
    class AppConnection(sqlite3.Connection):
        pass

    class AppCursor(sqlite3.Cursor):
        pass

    connection = sqlite3.connect(":memory:", factory=AppConnection)
    cursor = connection.cursor(AppCursor)

    trace = traced(lambda: cursor.execute("SELECT 1"))

    assert isinstance(connection, AppConnection)
    assert isinstance(cursor, AppCursor)
    assert sql_spans(trace) == [("sqlite3", "SELECT")]


def test_outside_a_trace_it_only_runs() -> None:
    connection = shop()

    assert connection.execute("SELECT count(*) FROM book").fetchone() == (3,)


def test_a_failing_statement_is_recorded_as_an_error() -> None:
    connection = shop()

    def work() -> None:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT * FROM missing")

    trace = traced(work)

    assert [(span.name, span.status) for span in statements(trace)] == [("SELECT missing", "error")]


# ---------- aiosqlite ----------


def test_aiosqlite_statements_are_recorded_once_each() -> None:
    aiosqlite = pytest.importorskip("aiosqlite")

    async def work() -> None:
        async with aiosqlite.connect(":memory:") as connection:
            await connection.execute("CREATE TABLE t (id INTEGER)")
            await connection.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT id FROM t WHERE id = ?", (2,))
                assert await cursor.fetchall() == [(2,)]

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("aiosqlite", "CREATE"), ("aiosqlite", "INSERT t"), ("aiosqlite", "SELECT t")]
    assert statements(trace)[2].attrs == IsPartialDict(
        {"db.params": [2], "code.origin": IsStr(regex=r"tests/integrations/dbapi/test_sqlite\.py:\d+ in work")}
    )


def test_sqlalchemy_on_aiosqlite_is_recorded_by_sqlalchemy_only() -> None:
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    async def work() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("sqlalchemy", "SELECT")]


AWAITED = IsStr(regex=r"tests/integrations/dbapi/test_sqlite\.py:\d+ in work")


def async_session_trace() -> Trace:
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415

    async def work() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        async with AsyncSession(engine) as session:
            await session.execute(text("SELECT 1"))
            await session.commit()
        await engine.dispose()

    return traced(lambda: asyncio.run(work()))


def test_async_sqlalchemy_names_the_code_that_awaited_the_statement() -> None:
    trace = async_session_trace()

    # SQLAlchemy runs the statement in a greenlet of its own: the code that awaits it waits in the greenlet above.
    assert [(span.kind, span.attrs["code.origin"]) for span in trace.spans[1:]] == [
        ("transaction", AWAITED),
        ("sql", AWAITED),
    ]


def test_async_sqlalchemy_names_the_library_code_that_awaited_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # As in Django's admin: no frame of the project's own on the stack.
    monkeypatch.setattr("tracepanel.core.frames._code_in_app", outside_the_project)

    (span,) = statements(async_session_trace())

    assert span.attrs["code.origin"] is None
    assert span.attrs["code.caller"] == IsStr(regex=r".*test_sqlite\.py:\d+ in .*work")


# ---------- the rows a statement returned ----------


def returned(trace: Trace) -> list[object]:
    return [span.attrs.get("db.response.returned_rows") for span in statements(trace)]


def test_sqlite3_counts_the_rows_as_they_are_fetched() -> None:
    connection = shop()

    def work() -> None:
        connection.execute("SELECT * FROM book").fetchall()
        connection.execute("SELECT * FROM book WHERE author_id = ?", (1,)).fetchone()
        cursor = connection.execute("SELECT * FROM book")
        cursor.fetchmany(2)
        cursor.fetchmany(2)
        list(connection.execute("SELECT * FROM author"))
        # Nothing fetched from it; an UPDATE returns none.
        connection.execute("SELECT * FROM book")
        connection.execute("UPDATE book SET title = 'w'").fetchall()

    assert returned(traced(work)) == [3, 1, 3, 2, None, None]


def test_aiosqlite_counts_the_rows_as_they_are_fetched() -> None:
    aiosqlite = pytest.importorskip("aiosqlite")

    async def work() -> None:
        async with aiosqlite.connect(":memory:") as connection:
            await connection.execute("CREATE TABLE t (id INTEGER)")
            await connection.executemany("INSERT INTO t VALUES (?)", [(n,) for n in range(5)])
            async with connection.execute("SELECT id FROM t") as cursor:
                await cursor.fetchall()
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT id FROM t WHERE id > ?", (1,))
                [row async for row in cursor]
                await cursor.execute("SELECT id FROM t WHERE id = ?", (0,))
                await cursor.fetchone()

    assert returned(traced(lambda: asyncio.run(work()))) == [None, None, 5, 3, 1]
