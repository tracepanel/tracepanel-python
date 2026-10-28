import asyncio

import pytest
from dirty_equals import IsPartialDict, IsStr

from tests.integrations.dbapi.conftest import explained_meanwhile, pg, pg_url, postgres, sql_spans, statements, traced
from tracepanel.core.model import Attrs, Trace
from tracepanel.integrations import dbapi
from tracepanel.integrations import sqlalchemy as sqlalchemy_integration

pytestmark = postgres

ORIGIN = IsStr(regex=r"tests/integrations/dbapi/test_postgres\.py:\d+ in work")


def dsn() -> str:
    return " ".join(f"{key}={value}" for key, value in pg().items())


# ---------- psycopg (3) ----------


def test_psycopg_statements_are_recorded_once_each() -> None:
    psycopg = pytest.importorskip("psycopg")

    with psycopg.connect(dsn()) as connection:

        def work() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s::int", (1,))
            connection.execute("SELECT %s::int", (2,))
            with connection.cursor(name="server") as cursor:
                cursor.execute("SELECT %s::int", (3,))
                cursor.fetchall()

        trace = traced(work)

    assert sql_spans(trace) == [("psycopg", "SELECT")] * 3
    assert statements(trace)[0].attrs == IsPartialDict(
        {
            "db.alias": f"psycopg:{pg()['dbname']}",
            "db.vendor": "postgresql",
            "db.statement": "SELECT %s::int",
            "db.params": [1],
            "code.origin": ORIGIN,
        }
    )


def test_psycopg_composed_sql_is_recorded_as_the_server_gets_it() -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql  # noqa: PLC0415

    with psycopg.connect(dsn()) as connection:
        query = sql.SQL("SELECT {} FROM {}").format(sql.Identifier("oid"), sql.Identifier("pg_type"))
        trace = traced(lambda: connection.execute(query))

    assert statements(trace)[0].attrs["db.statement"] == 'SELECT "oid" FROM "pg_type"'


def test_psycopg_async_statements_are_recorded() -> None:
    psycopg = pytest.importorskip("psycopg")

    async def work() -> None:
        async with await psycopg.AsyncConnection.connect(dsn()) as connection:
            await connection.execute("SELECT %s::int", (1,))
            async with connection.cursor() as cursor:
                await cursor.executemany("SELECT %s::int", [(1,), (2,)])

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("psycopg", "SELECT"), ("psycopg", "SELECT")]
    assert [span.attrs["db.many"] for span in statements(trace)] == [False, True]
    assert statements(trace)[0].attrs == IsPartialDict({"code.origin": ORIGIN})


def test_psycopg_statement_can_be_explained() -> None:
    psycopg = pytest.importorskip("psycopg")

    with psycopg.connect(dsn()) as connection:
        trace = traced(lambda: connection.execute("SELECT oid FROM pg_type WHERE typname = %s", ("int4",)))
    alias = str(statements(trace)[0].attrs["db.alias"])

    plan = dbapi.explain("SELECT oid FROM pg_type WHERE typname = %s", ["int4"], alias)

    assert plan.to_dict() == IsPartialDict(vendor="postgresql", alias=alias)


# ---------- psycopg2 ----------


def test_psycopg2_statements_are_recorded_once_each() -> None:
    pytest.importorskip("psycopg2")
    import psycopg2.extras  # noqa: PLC0415

    connection = psycopg2.connect(dsn())

    def work() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT %s::int", (1,))
            cursor.executemany("SELECT %s::int", [(1,), (2,)])
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SELECT %(n)s::int AS n", {"n": 2})
            assert cursor.fetchall() == [{"n": 2}]

    try:
        trace = traced(work)
    finally:
        connection.close()

    assert sql_spans(trace) == [("psycopg2", "SELECT")] * 3
    assert [span.attrs["db.many"] for span in statements(trace)] == [False, True, False]
    assert statements(trace)[0].attrs == IsPartialDict(
        {"db.alias": f"psycopg2:{pg()['dbname']}", "db.params": [1], "code.origin": ORIGIN}
    )


def test_psycopg2_keeps_the_app_s_factories() -> None:
    pytest.importorskip("psycopg2")
    import psycopg2.extensions  # noqa: PLC0415
    import psycopg2.extras  # noqa: PLC0415

    class AppConnection(psycopg2.extensions.connection):
        pass

    connection = psycopg2.connect(dsn(), connection_factory=AppConnection, cursor_factory=psycopg2.extras.DictCursor)
    try:
        cursor = connection.cursor()
        trace = traced(lambda: cursor.execute("SELECT 1"))

        assert isinstance(connection, AppConnection)
        assert isinstance(cursor, psycopg2.extras.DictCursor)
        assert sql_spans(trace) == [("psycopg2", "SELECT")]
    finally:
        connection.close()


def test_psycopg2_statement_can_be_explained() -> None:
    psycopg2 = pytest.importorskip("psycopg2")

    connection = psycopg2.connect(dsn())
    try:
        trace = traced(lambda: connection.cursor().execute("SELECT oid FROM pg_type WHERE typname = %s", ("int4",)))
    finally:
        connection.close()
    alias = str(statements(trace)[0].attrs["db.alias"])

    assert dbapi.explain("SELECT oid FROM pg_type WHERE typname = %s", ["int4"], alias).to_dict() == IsPartialDict(
        vendor="postgresql"
    )


# ---------- asyncpg ----------


def asyncpg_params() -> dict[str, object]:
    p = pg()
    return {
        "host": p["host"],
        "port": int(p["port"]),
        "user": p["user"],
        "password": p["password"],
        "database": p["dbname"],
    }


def test_asyncpg_statements_are_recorded_once_each() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def work() -> None:
        connection = await asyncpg.connect(**asyncpg_params())
        try:
            await connection.fetch("SELECT $1::int", 1)
            await connection.fetchrow("SELECT $1::int", 2)
            await connection.fetchval("SELECT $1::int", 3)
            await connection.execute("SELECT 1")
            await connection.executemany("SELECT $1::int", [(1,), (2,)])
        finally:
            await connection.close()

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("asyncpg", "SELECT")] * 5
    assert [span.attrs.get("db.params") for span in statements(trace)] == [[1], [2], [3], None, None]
    assert statements(trace)[0].attrs == IsPartialDict(
        {"db.alias": f"asyncpg:{pg()['dbname']}", "server.address": pg()["host"], "code.origin": ORIGIN}
    )


def test_asyncpg_pool_statements_are_recorded_once_and_its_resets_not_at_all() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def work() -> None:
        pool = await asyncpg.create_pool(**asyncpg_params(), min_size=1, max_size=1)
        try:
            await pool.fetchval("SELECT $1::int", 1)
            async with pool.acquire() as connection:
                await connection.fetchval("SELECT $1::int", 2)
        finally:
            await pool.close()

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("asyncpg", "SELECT"), ("asyncpg", "SELECT")]


def test_asyncpg_statement_can_be_explained() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def work() -> None:
        connection = await asyncpg.connect(**asyncpg_params())
        try:
            await connection.fetch("SELECT oid FROM pg_type WHERE typname = $1", "int4")
        finally:
            await connection.close()

    trace = traced(lambda: asyncio.run(work()))
    alias = str(statements(trace)[0].attrs["db.alias"])

    assert dbapi.explain("SELECT oid FROM pg_type WHERE typname = $1", ["int4"], alias).to_dict() == IsPartialDict(
        vendor="postgresql"
    )


# ---------- SQLAlchemy on each driver: SQLAlchemy's statements, once ----------


@pytest.mark.parametrize("dialect", ["psycopg", "psycopg2"])
def test_sqlalchemy_on_a_postgres_driver_is_recorded_by_sqlalchemy_only(dialect: str) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip(dialect)
    engine = sqlalchemy.create_engine(pg_url(dialect))

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))

    try:
        assert sql_spans(traced(work)) == [("sqlalchemy", "SELECT")]
    finally:
        engine.dispose()


@pytest.mark.parametrize("dialect", ["asyncpg", "psycopg"])
def test_async_sqlalchemy_on_a_postgres_driver_is_recorded_by_sqlalchemy_only(dialect: str) -> None:
    pytest.importorskip(dialect)
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    async def work() -> None:
        engine = create_async_engine(pg_url(dialect))
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()

    assert sql_spans(traced(lambda: asyncio.run(work()))) == [("sqlalchemy", "SELECT")]


@pytest.mark.parametrize("dialect", ["asyncpg", "psycopg"])
def test_async_sqlalchemy_statement_can_be_explained_and_names_its_caller(dialect: str) -> None:
    pytest.importorskip(dialect)
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415

    plan: dict[str, object] = {}

    async def work() -> None:
        engine = create_async_engine(pg_url(dialect))
        async with AsyncSession(engine) as session:
            await session.execute(text("SELECT oid FROM pg_type WHERE typname = :name"), {"name": "int4"})
            # asyncpg's connections belong to the loop they were made on: EXPLAIN makes its own.
            plan.update(await explained_meanwhile())
            await session.execute(text("SELECT 1"))
        await engine.dispose()

    trace = traced(lambda: asyncio.run(work()))

    assert plan == {
        "vendor": "postgresql",
        "alias": statements(trace)[0].attrs["db.alias"],
        "analyzed": False,
        "lines": [
            {"depth": 0, "text": IsStr(regex=r"Index Scan using \w+ on pg_type .*"), "warning": None},
            {"depth": 1, "text": "Index Cond: (typname = 'int4'::name)", "warning": None},
        ],
    }

    assert [(span.status, span.attrs["code.origin"]) for span in statements(trace)] == [("ok", ORIGIN), ("ok", ORIGIN)]


# ---------- the rows a statement returned ----------


def returned(trace: Trace) -> list[object]:
    return [span.attrs.get("db.response.returned_rows") for span in statements(trace)]


def test_asyncpg_says_how_many_rows_a_query_returned() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def work() -> None:
        connection = await asyncpg.connect(**asyncpg_params())
        try:
            await connection.fetch("SELECT generate_series(1, $1::int)", 7)
            await connection.fetchrow("SELECT generate_series(1, $1::int)", 7)
            await connection.fetchrow("SELECT 1 WHERE false")
            await connection.execute("SELECT generate_series(1, 4)")
            # fetchval reads one value, whatever the rows; executemany returns none.
            await connection.fetchval("SELECT generate_series(1, 4)")
            await connection.executemany("SELECT $1::int", [(1,), (2,)])
        finally:
            await connection.close()

    assert returned(traced(lambda: asyncio.run(work()))) == [7, 1, 0, 4, None, None]


def test_psycopg_async_says_how_many_rows_a_query_returned() -> None:
    psycopg = pytest.importorskip("psycopg")

    async def work() -> None:
        async with await psycopg.AsyncConnection.connect(dsn()) as connection:
            await (await connection.execute("SELECT generate_series(1, 9)")).fetchall()
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT generate_series(1, 3)")

    assert returned(traced(lambda: asyncio.run(work()))) == [9, 3]


def test_async_sqlalchemy_counts_the_rows_of_a_result() -> None:
    pytest.importorskip("asyncpg")
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    async def work() -> None:
        engine = create_async_engine(pg_url("asyncpg"))
        async with engine.connect() as connection:
            (await connection.execute(text("SELECT generate_series(1, 6)"))).all()
            [row async for row in await connection.stream(text("SELECT generate_series(1, 5)"))]
        await engine.dispose()

    assert returned(traced(lambda: asyncio.run(work()))) == [6, 5]


def test_asyncpg_prepared_statements_are_recorded_with_their_rows() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def work() -> None:
        connection = await asyncpg.connect(**asyncpg_params())
        try:
            statement = await connection.prepare("SELECT generate_series(1, $1::int)")
            await statement.fetch(8)
            await statement.fetchrow(8)
            await statement.fetchval(8)
            await statement.fetchmany([(2,), (3,)])
        finally:
            await connection.close()

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("asyncpg", "SELECT")] * 4
    assert [span.attrs.get("db.params") for span in statements(trace)] == [[8], [8], [8], None]
    assert [span.attrs["db.many"] for span in statements(trace)] == [False, False, False, True]
    assert returned(trace) == [8, 1, None, 5]
    assert statements(trace)[0].attrs == IsPartialDict(
        {"db.statement": "SELECT generate_series(1, $1::int)", "code.origin": ORIGIN}
    )


def test_asyncpg_cursors_are_recorded_once_with_every_row_read() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def work() -> None:
        connection = await asyncpg.connect(**asyncpg_params())
        try:
            async with connection.transaction():
                # Read 3 at a time: one statement, every chunk counted.
                async for _ in connection.cursor("SELECT generate_series(1, $1::int)", 10, prefetch=3):
                    pass
                cursor = await connection.cursor("SELECT generate_series(1, 20)")
                await cursor.fetch(4)
                await cursor.fetchrow()
                # Moved over, not returned.
                await cursor.forward(5)
                statement = await connection.prepare("SELECT generate_series(1, 6)")
                [row async for row in statement.cursor()]
        finally:
            await connection.close()

    trace = traced(lambda: asyncio.run(work()))
    selects = [span for span in statements(trace) if span.name == "SELECT"]

    assert [span.attrs.get("db.params") for span in selects] == [[10], None, None]
    assert [span.attrs.get("db.response.returned_rows") for span in selects] == [10, 5, 6]
    assert selects[0].attrs == IsPartialDict({"code.origin": ORIGIN})


def test_psycopg_server_side_cursor_counts_the_rows_read() -> None:
    psycopg = pytest.importorskip("psycopg")

    def work() -> None:
        with psycopg.connect(dsn()) as connection, connection.cursor(name="big") as cursor:
            cursor.execute("SELECT generate_series(1, 12)")
            cursor.fetchmany(5)
            cursor.fetchone()
            list(cursor)

    assert returned(traced(work)) == [12]


def test_psycopg_async_server_side_cursor_counts_the_rows_read() -> None:
    psycopg = pytest.importorskip("psycopg")

    async def work() -> None:
        async with await psycopg.AsyncConnection.connect(dsn()) as connection, connection.cursor(name="big") as cursor:
            await cursor.execute("SELECT generate_series(1, 7)")
            [row async for row in cursor]

    assert returned(traced(lambda: asyncio.run(work()))) == [7]


# ---------- opening a connection ----------


def connections(trace: Trace) -> list[Attrs]:
    """The attributes of each `sql.connect` span, in order."""
    return [span.attrs for span in trace.spans if span.kind == "sql.connect"]


def opened(client: str, **more: object) -> dict[str, object]:
    """What a connection's span says about it: the database it opened, its server, the app's line that opened it."""
    return {
        "db.client": client,
        "db.alias": f"{client}:{pg()['dbname']}",
        "db.vendor": "postgresql",
        "db.system": "postgresql",
        "db.namespace": pg()["dbname"],
        "server.address": pg()["host"],
        "server.port": int(pg()["port"]),
        "code.origin": ORIGIN,
        **more,
    }


def test_a_connection_each_driver_opens_is_a_connect_span() -> None:
    psycopg, psycopg2 = pytest.importorskip("psycopg"), pytest.importorskip("psycopg2")
    asyncpg = pytest.importorskip("asyncpg")

    async def asynchronous() -> None:
        await (await psycopg.AsyncConnection.connect(dsn())).close()
        await (await asyncpg.connect(**asyncpg_params())).close()

    def work() -> None:
        psycopg.connect(dsn()).close()
        psycopg2.connect(dsn()).close()
        asyncio.run(asynchronous())

    trace = traced(work)

    assert [span.name for span in trace.spans if span.kind == "sql.connect"] == ["connect postgresql"] * 4
    meanwhile = IsStr(regex=r"tests/integrations/dbapi/test_postgres\.py:\d+ in asynchronous")
    assert connections(trace) == [
        opened("psycopg"),
        opened("psycopg2"),
        opened("psycopg", **{"code.origin": meanwhile}),
        opened("asyncpg", **{"code.origin": meanwhile}),
    ]


def test_a_connection_asyncpg_s_pool_opens_says_so() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def work() -> None:
        pool = await asyncpg.create_pool(**asyncpg_params(), min_size=1, max_size=1)
        await pool.close()

    trace = traced(lambda: asyncio.run(work()))

    assert connections(trace) == [opened("asyncpg", **{"db.connection.pool": "asyncpg"})]


@pytest.mark.parametrize("dialect", ["psycopg", "psycopg2"])
def test_a_connection_sqlalchemy_s_pool_opens_is_sqlalchemy_s(dialect: str) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip(dialect)
    engine = sqlalchemy.create_engine(pg_url(dialect))

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))

    try:
        cold, warm = traced(work), traced(work)
    finally:
        engine.dispose()

    assert connections(cold) == [
        {
            "db.client": "sqlalchemy",
            "db.alias": sqlalchemy_integration.engine_alias(engine),
            "db.vendor": "postgresql",
            "db.system": "postgresql",
            "db.namespace": pg()["dbname"],
            "server.address": pg()["host"],
            "server.port": int(pg()["port"]),
            "db.connection.pool": "sqlalchemy",
            "code.origin": ORIGIN,
        }
    ]
    assert connections(warm) == []


@pytest.mark.parametrize("dialect", ["asyncpg", "psycopg"])
def test_a_connection_async_sqlalchemy_opens_is_one_span(dialect: str) -> None:
    pytest.importorskip(dialect)
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    async def work() -> None:
        engine = create_async_engine(pg_url(dialect))
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()

    trace = traced(lambda: asyncio.run(work()))

    assert connections(trace) == [IsPartialDict({"db.client": "sqlalchemy", "db.connection.pool": "sqlalchemy"})]
