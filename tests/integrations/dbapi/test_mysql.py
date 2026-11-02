import asyncio
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from dirty_equals import IsList, IsPartialDict, IsStr

from tests.integrations.dbapi.conftest import explained_meanwhile, my, my_url, mysql, sql_spans, statements, traced
from tracepanel.integrations import dbapi
from tracepanel.integrations import sqlalchemy as sqlalchemy_integration

pytestmark = mysql

ORIGIN = IsStr(regex=r"tests/integrations/dbapi/test_mysql\.py:\d+ in work")


def pymysql_connect() -> Any:  # noqa: ANN401
    return pytest.importorskip("pymysql").connect(**my())


def mysqlclient_connect() -> Any:  # noqa: ANN401
    m = my()
    return pytest.importorskip("MySQLdb").connect(
        host=m["host"], port=m["port"], user=m["user"], password=m["password"], database=m["database"]
    )


def connector_connect(*, pure: bool) -> Callable[[], Any]:
    def connect() -> Any:  # noqa: ANN401
        connector = pytest.importorskip("mysql.connector")
        if not pure and not connector.HAVE_CEXT:
            pytest.skip("MySQL Connector's C extension isn't built for this Python")
        return connector.connect(**my(), use_pure=pure)

    return connect


DRIVERS = {
    "pymysql": pymysql_connect,
    "mysqlclient": mysqlclient_connect,
    "mysql-connector": connector_connect(pure=True),
    # The C extension's cursor classes: wrapped apart from the pure Python ones.
    "mysql-connector-c": connector_connect(pure=False),
}


@pytest.mark.parametrize("driver", DRIVERS)
def test_statements_are_recorded_once_each(driver: str) -> None:
    connection = DRIVERS[driver]()
    client = driver.removesuffix("-c")

    def work() -> None:
        cursor = connection.cursor()
        cursor.execute("SELECT %s", (1,))
        cursor.fetchall()
        # Not an INSERT: the drivers run executemany() as execute() once per row, inside it.
        cursor.executemany("SELECT %s", [(1,), (2,)])
        cursor.close()

    try:
        trace = traced(work)
    finally:
        connection.close()

    assert sql_spans(trace) == [(client, "SELECT"), (client, "SELECT")]
    assert [span.attrs["db.many"] for span in statements(trace)] == [False, True]
    assert statements(trace)[0].attrs == IsPartialDict(
        {
            "db.client": client,
            "db.alias": f"{client}:{my()['database']}",
            "db.vendor": "mysql",
            "db.system": "mysql",
            "db.namespace": my()["database"],
            "db.statement": "SELECT %s",
            "db.params": [1],
            "code.origin": ORIGIN,
        }
    )


@pytest.mark.parametrize("driver", DRIVERS)
def test_a_statement_can_be_explained(driver: str) -> None:
    connection = DRIVERS[driver]()

    def work() -> None:
        cursor = connection.cursor()
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", ("mysql",))
        cursor.fetchall()
        cursor.close()

    try:
        trace = traced(work)
    finally:
        connection.close()
    alias = str(statements(trace)[0].attrs["db.alias"])

    plan = dbapi.explain("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", ["mysql"], alias)

    assert plan.to_dict() == IsPartialDict(vendor="mysql", alias=alias)


def test_aiomysql_statements_are_recorded_once_each() -> None:
    aiomysql = pytest.importorskip("aiomysql")
    m = my()

    async def work() -> None:
        connection = await aiomysql.connect(
            host=m["host"], port=m["port"], user=m["user"], password=m["password"], db=m["database"]
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT %s", (1,))
                await cursor.executemany("SELECT %s", [(1,), (2,)])
        finally:
            connection.close()

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("aiomysql", "SELECT"), ("aiomysql", "SELECT")]
    assert statements(trace)[0].attrs == IsPartialDict({"db.params": [1], "code.origin": ORIGIN})


def test_asyncmy_statements_are_recorded_once_each_whatever_cursor_class() -> None:
    asyncmy = pytest.importorskip("asyncmy")
    from asyncmy.cursors import DictCursor  # noqa: PLC0415

    m = my()

    async def work() -> None:
        connection = await asyncmy.connect(
            host=m["host"], port=m["port"], user=m["user"], password=m["password"], database=m["database"]
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT %s", (1,))
                await cursor.executemany("SELECT %s", [(1,), (2,)])
            async with connection.cursor(DictCursor) as cursor:
                await cursor.execute("SELECT %s AS one", (1,))
                assert await cursor.fetchone() == {"one": 1}
        finally:
            connection.close()

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("asyncmy", "SELECT")] * 3
    assert statements(trace)[0].attrs == IsPartialDict({"db.params": [1], "code.origin": ORIGIN})


def test_mysql_connector_asyncio_statements_are_recorded() -> None:
    pytest.importorskip("mysql.connector")
    from mysql.connector.aio import connect  # noqa: PLC0415

    async def work() -> None:
        # pyrefly: ignore[bad-context-manager] (typed as either a connection or a pooled one; this is a connection)
        async with await connect(**my()) as connection, await connection.cursor() as cursor:
            await cursor.execute("SELECT %s", (1,))
            await cursor.fetchall()

    trace = traced(lambda: asyncio.run(work()))

    assert sql_spans(trace) == [("mysql-connector", "SELECT")]


# ---------- SQLAlchemy on each driver: SQLAlchemy's statements, once ----------


@pytest.mark.parametrize("dialect", ["pymysql", "mysqldb", "mysqlconnector"])
def test_sqlalchemy_on_a_mysql_driver_is_recorded_by_sqlalchemy_only(dialect: str) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine(my_url(dialect))

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))

    try:
        assert sql_spans(traced(work)) == [("sqlalchemy", "SELECT")]
    finally:
        engine.dispose()


def test_async_sqlalchemy_on_aiomysql_is_recorded_by_sqlalchemy_only() -> None:
    pytest.importorskip("aiomysql")
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    async def work() -> None:
        engine = create_async_engine(my_url("aiomysql"))
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()

    assert sql_spans(traced(lambda: asyncio.run(work()))) == [("sqlalchemy", "SELECT")]


@pytest.mark.parametrize("dialect", ["aiomysql", "asyncmy"])
def test_async_sqlalchemy_statement_can_be_explained_and_names_its_caller(dialect: str) -> None:
    pytest.importorskip(dialect)
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415

    plan: dict[str, object] = {}

    async def work() -> None:
        engine = create_async_engine(my_url(dialect))
        async with AsyncSession(engine) as session:
            await session.execute(text("SELECT :n + 1"), {"n": 1})
            plan.update(await explained_meanwhile())
            await session.execute(text("SELECT 1"))
        await engine.dispose()

    trace = traced(lambda: asyncio.run(work()))

    assert plan == {
        "vendor": "mysql",
        "alias": statements(trace)[0].attrs["db.alias"],
        "analyzed": False,
        "lines": [{"depth": 0, "text": IsStr(regex=r"id=1  select_type=SIMPLE.*No tables used"), "warning": None}],
    }

    assert [(span.status, span.attrs["code.origin"]) for span in statements(trace)] == [("ok", ORIGIN), ("ok", ORIGIN)]


@pytest.mark.parametrize("driver", ["pymysql", "mysqlclient"])
def test_a_connection_the_driver_opens_is_a_connect_span(driver: str) -> None:
    m = my()

    def work() -> None:
        DRIVERS[driver]().close()

    trace = traced(work)

    assert [(span.name, span.attrs) for span in trace.spans if span.kind == "sql.connect"] == [
        (
            "connect mysql",
            {
                "db.client": driver,
                "db.alias": f"{driver}:{m['database']}",
                "db.vendor": "mysql",
                "db.system": "mysql",
                "db.namespace": m["database"],
                "server.address": m["host"],
                "server.port": m["port"],
                "code.origin": IsStr(regex=rf"tests/integrations/dbapi/test_mysql\.py:\d+ in {driver}_connect"),
            },
        )
    ]


# ---------- EXPLAIN ANALYZE runs the query: whatever it does is rolled back ----------


@pytest.fixture
def side_effect() -> Iterator[tuple[str, Callable[[], int]]]:
    """A CTE whose SELECT inserts a row (a function that modifies data), and how many rows its table holds."""
    pymysql = pytest.importorskip("pymysql")
    name = f"explained_{uuid.uuid4().hex[:12]}"
    connection = pymysql.connect(**my(), autocommit=True)
    try:
        with connection.cursor() as cursor:
            # With the binary log on, MySQL takes a function that writes only from a trusted creator.
            cursor.execute("SET GLOBAL log_bin_trust_function_creators = 1")
            cursor.execute(f"CREATE TABLE {name} (id INT AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB")
            cursor.execute(
                f"CREATE FUNCTION {name}_add() RETURNS INT NOT DETERMINISTIC MODIFIES SQL DATA "
                f"BEGIN INSERT INTO {name} () VALUES (); RETURN 1; END"
            )

        def rows() -> int:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT count(*) FROM {name}")
                (count,) = cursor.fetchone()
                return int(count)

        yield f"WITH added AS (SELECT {name}_add() AS n) SELECT n FROM added", rows
        with connection.cursor() as cursor:
            cursor.execute(f"DROP FUNCTION {name}_add")
            cursor.execute(f"DROP TABLE {name}")
    finally:
        connection.close()


ANALYZED = {
    "vendor": "mysql",
    "alias": IsStr(),
    "analyzed": True,
    "lines": IsList(
        IsPartialDict(depth=0, text=IsStr(regex=r"(?s)Table scan on added .*actual time.*")), length=(1, ...)
    ),
}


@pytest.mark.parametrize("driver", DRIVERS)
def test_analyze_of_a_cte_is_rolled_back(driver: str, side_effect: tuple[str, Callable[[], int]]) -> None:
    statement, rows = side_effect
    connection = DRIVERS[driver]()

    def work() -> None:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        cursor.close()

    try:
        trace = traced(work)
    finally:
        connection.close()

    plan = dbapi.explain(statement, [], str(statements(trace)[0].attrs["db.alias"]), analyze=True)

    assert plan.to_dict() == ANALYZED
    assert rows() == 0


@pytest.mark.parametrize("dialect", ["pymysql", "mysqldb"])
def test_sqlalchemy_analyze_is_rolled_back(dialect: str, side_effect: tuple[str, Callable[[], int]]) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("MySQLdb" if dialect == "mysqldb" else dialect)
    statement, rows = side_effect
    engine = sqlalchemy.create_engine(my_url(dialect))
    try:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))

        plan = sqlalchemy_integration.explain(statement, [], sqlalchemy_integration.engine_alias(engine), analyze=True)
    finally:
        engine.dispose()

    assert plan.to_dict() == ANALYZED
    assert rows() == 0


@pytest.mark.parametrize("dialect", ["aiomysql", "asyncmy"])
def test_async_sqlalchemy_analyze_is_rolled_back(dialect: str, side_effect: tuple[str, Callable[[], int]]) -> None:
    pytest.importorskip(dialect)
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    statement, rows = side_effect
    plan: dict[str, object] = {}

    async def work() -> None:
        engine = create_async_engine(my_url(dialect))
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            alias = sqlalchemy_integration.engine_alias(engine.sync_engine)
            explained = await asyncio.to_thread(sqlalchemy_integration.explain, statement, [], alias, analyze=True)
            plan.update(explained.to_dict())
        await engine.dispose()

    asyncio.run(work())

    assert plan == ANALYZED
    assert rows() == 0
