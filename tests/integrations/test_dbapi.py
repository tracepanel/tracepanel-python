import sqlite3
from collections.abc import Callable

import pytest
from dirty_equals import IsPartialDict, IsStr

from tracepanel.core.context import start_trace
from tracepanel.core.model import Trace
from tracepanel.integrations import dbapi
from tracepanel.integrations import sqlalchemy as sqlalchemy_integration
from tracepanel.storage.transport import complete

# Installed for the rest of the session, as the other integrations are: every Django and SQLAlchemy test that runs
# after this module then runs on the wrapped drivers, and would see any statement recorded twice.
dbapi.install()
sqlalchemy_integration.install()


def traced(work: Callable[[], object]) -> Trace:
    with start_trace("http", "GET /") as trace:
        work()
    return complete(trace)


def sql_spans(trace: Trace) -> list[tuple[str, str]]:
    return [(str(span.attrs["db.client"]), span.name) for span in trace.spans if span.kind == "sql"]


def shop(path: str = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER)")
    connection.executemany("INSERT INTO author (id, name) VALUES (?, ?)", [(1, "a"), (2, "b")])
    connection.executemany("INSERT INTO book (title, author_id) VALUES (?, ?)", [("x", 1), ("y", 2), ("z", 1)])
    return connection


# ---------- sqlite3 on its own ----------


def test_sqlite3_statements_are_recorded_once_each() -> None:
    connection = shop()

    def work() -> None:
        books = connection.execute("SELECT title, author_id FROM book").fetchall()
        for _, author_id in books:
            connection.cursor().execute("SELECT name FROM author WHERE id = ?", (author_id,)).fetchone()

    trace = traced(work)

    assert sql_spans(trace) == [("sqlite3", "SELECT book")] + [("sqlite3", "SELECT author")] * 3
    lookup = next(span for span in trace.spans if span.name == "SELECT author")
    assert lookup.attrs == IsPartialDict(
        {
            "db.client": "sqlite3",
            "db.vendor": "sqlite",
            "db.system": "sqlite",
            "db.statement": "SELECT name FROM author WHERE id = ?",
            "db.fingerprint": "SELECT name FROM author WHERE id = ?",
            "db.params": [1],
            "db.many": False,
            "code.origin": IsStr(regex=r"tests/integrations/test_dbapi\.py:\d+ in work"),
        }
    )


def test_sqlite3_parameters_tell_repeats_from_a_loop() -> None:
    connection = shop()

    def work() -> None:
        for author_id in (1, 1, 2):
            connection.execute("SELECT name FROM author WHERE id = ?", (author_id,))

    hashes = [span.attrs["db.params_hash"] for span in traced(work).spans if span.kind == "sql"]

    assert hashes[0] == hashes[1] != hashes[2]


def test_sqlite3_executemany_and_executescript_are_one_statement_each() -> None:
    connection = shop()

    def work() -> None:
        connection.executemany("INSERT INTO author (name) VALUES (?)", [("c",), ("d",)])
        connection.executescript("DELETE FROM book; DELETE FROM author;")

    trace = traced(work)

    assert sql_spans(trace) == [("sqlite3", "INSERT author"), ("sqlite3", "DELETE book")]
    assert [span.attrs["db.many"] for span in trace.spans if span.kind == "sql"] == [True, False]


def test_sqlite3_names_the_database_after_its_file(tmp_path: object) -> None:
    connection = shop(str(tmp_path) + "/shop.sqlite3")

    trace = traced(lambda: connection.execute("SELECT count(*) FROM book"))

    assert trace.spans[-1].attrs == IsPartialDict({"db.alias": "shop.sqlite3", "db.namespace": "shop.sqlite3"})


def test_sqlite3_keeps_the_app_s_factories() -> None:
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


def test_sqlite3_outside_a_trace_only_runs() -> None:
    connection = shop()

    assert connection.execute("SELECT count(*) FROM book").fetchone() == (3,)


def test_a_failing_statement_is_recorded_as_an_error() -> None:
    connection = shop()

    def work() -> None:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT * FROM missing")

    trace = traced(work)

    assert [(span.name, span.status) for span in trace.spans if span.kind == "sql"] == [("SELECT missing", "error")]


# ---------- under an ORM: the ORM's, never twice ----------


def test_sqlalchemy_on_sqlite3_is_recorded_by_sqlalchemy_only() -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite://")

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))
            connection.exec_driver_sql("SELECT 2")

    trace = traced(work)

    assert sql_spans(trace) == [("sqlalchemy", "SELECT"), ("sqlalchemy", "SELECT")]
    # The engine's connection is the traced driver's: the guard, not a missing wrapper, kept it out.
    with engine.connect() as connection:
        assert type(connection.connection.dbapi_connection).__name__.startswith("Traced")


def test_sqlalchemy_explain_is_not_recorded_as_the_app_s_statement() -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite://", logging_name="explained")
    sqlalchemy_integration.engine_alias(engine)

    trace = traced(lambda: sqlalchemy_integration.explain("SELECT 1", [], "explained"))

    assert sql_spans(trace) == []


@pytest.mark.django_db
def test_django_orm_is_recorded_by_django_only() -> None:
    from django.db import connections  # noqa: PLC0415

    # A connection opened now, through the wrapped driver (the test's own may predate it).
    connection = connections.create_connection("default")

    def work() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

    try:
        trace = traced(work)
        clients = [client for client, _ in sql_spans(trace)]
        assert clients == ["django"]
        if connection.vendor == "sqlite":
            assert type(connection.connection).__name__.startswith("Traced")
    finally:
        connection.close()


# ---------- psycopg (the Postgres pairs of the test matrix) ----------

postgres = pytest.mark.skipif(
    __import__("os").environ.get("TRACEPANEL_TEST_DB") != "postgres", reason="needs the Postgres test database"
)


def postgres_dsn() -> str:
    import os  # noqa: PLC0415

    return (
        f"dbname={os.environ.get('PGDATABASE', 'tracepanel')} user={os.environ.get('PGUSER', 'postgres')} "
        f"password={os.environ.get('PGPASSWORD', 'postgres')} host={os.environ.get('PGHOST', 'localhost')} "
        f"port={os.environ.get('PGPORT', '5432')}"
    )


@postgres
def test_psycopg_statements_are_recorded_once_each() -> None:
    psycopg = pytest.importorskip("psycopg")

    with psycopg.connect(postgres_dsn()) as connection:

        def work() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s::int", (1,))
            connection.execute("SELECT %s::int", (2,))

        trace = traced(work)

    assert sql_spans(trace) == [("psycopg", "SELECT"), ("psycopg", "SELECT")]
    first = next(span for span in trace.spans if span.kind == "sql")
    assert first.attrs == IsPartialDict(
        {"db.vendor": "postgresql", "db.statement": "SELECT %s::int", "db.params": [1]}
    )


@postgres
def test_psycopg_async_statements_are_recorded() -> None:
    import asyncio  # noqa: PLC0415

    psycopg = pytest.importorskip("psycopg")

    async def work() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_dsn()) as connection:
            await connection.execute("SELECT 1")

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("psycopg", "SELECT")]


@postgres
def test_sqlalchemy_on_psycopg_is_recorded_by_sqlalchemy_only() -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg")
    import os  # noqa: PLC0415

    url = (
        f"postgresql+psycopg://{os.environ.get('PGUSER', 'postgres')}:{os.environ.get('PGPASSWORD', 'postgres')}"
        f"@{os.environ.get('PGHOST', 'localhost')}:{os.environ.get('PGPORT', '5432')}"
        f"/{os.environ.get('PGDATABASE', 'tracepanel')}"
    )
    engine = sqlalchemy.create_engine(url)

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))

    try:
        assert sql_spans(traced(work)) == [("sqlalchemy", "SELECT")]
    finally:
        engine.dispose()
