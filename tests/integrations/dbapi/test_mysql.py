import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from dirty_equals import IsPartialDict, IsStr

from tests.integrations.dbapi.conftest import my, my_url, mysql, sql_spans, statements, traced
from tracepanel.integrations import dbapi

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
