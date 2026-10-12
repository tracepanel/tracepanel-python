import asyncio
import os

import pytest
from dirty_equals import IsPartialDict

from tests.integrations.dbapi.conftest import oracle, sql_spans, statements, traced
from tracepanel.core.explain import ExplainError
from tracepanel.integrations import dbapi

pytestmark = oracle

oracledb = pytest.importorskip("oracledb")


def params() -> dict[str, str]:
    return {
        "user": os.environ.get("ORACLE_USER", "tracepanel"),
        "password": os.environ.get("ORACLE_PASSWORD", "tracepanel"),
        "dsn": os.environ.get("ORACLE_DSN", "localhost:1521/FREEPDB1"),
    }


def test_statements_are_recorded_once_each() -> None:
    connection = oracledb.connect(**params())

    def work() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT :n FROM dual", {"n": 1})
            cursor.fetchall()
            cursor.execute("SELECT :1 FROM dual", [2])
            cursor.fetchall()

    try:
        trace = traced(work)
    finally:
        connection.close()

    host, _, rest = params()["dsn"].partition(":")
    assert sql_spans(trace) == [("oracledb", "SELECT dual"), ("oracledb", "SELECT dual")]
    assert statements(trace)[0].attrs == IsPartialDict(
        {
            "db.vendor": "oracle",
            "db.alias": f"oracledb:{rest.partition('/')[2]}",
            "db.params": [1],
            "db.param_names": ["n"],
            "server.address": host,
        }
    )


def test_async_statements_are_recorded() -> None:
    async def work() -> None:
        async with oracledb.connect_async(**params()) as connection, connection.cursor() as cursor:
            await cursor.execute("SELECT 1 FROM dual")
            await cursor.fetchall()

    assert sql_spans(traced(lambda: asyncio.run(work()))) == [("oracledb", "SELECT dual")]


def test_explain_says_it_is_not_available() -> None:
    connection = oracledb.connect(**params())
    try:
        trace = traced(lambda: connection.cursor().execute("SELECT 1 FROM dual"))
    finally:
        connection.close()

    with pytest.raises(ExplainError, match="isn't available for queries made through oracledb"):
        dbapi.explain("SELECT 1 FROM dual", [], str(statements(trace)[0].attrs["db.alias"]))
