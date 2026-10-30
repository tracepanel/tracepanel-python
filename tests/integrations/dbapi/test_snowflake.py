"""Snowflake's connector against a local stand-in for its REST API (snowflakestub): no account.

The `snowflake` test group has 4.x, whose own copy of requests and urllib3 (aiohttp for asyncio) makes its HTTP calls.
The `snowflake5` group has 5.x, whose Rust core makes them out of Python's sight. Either way the connection and the
statements show, and the connector's HTTP calls don't: their time is the span's they serve. The app's own calls show.
"""

import asyncio
import importlib
from collections.abc import Iterator
from typing import Any, cast

import pytest
from dirty_equals import IsInt, IsPartialDict, IsStr
from jsonschema import Draft202012Validator

from tests.integrations.dbapi import snowflakestub
from tests.integrations.dbapi.conftest import traced
from tracepanel._compat import override
from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.integrations import http_clients

pytest.importorskip("snowflake.connector")
http_clients.install()

ORIGIN = IsStr(regex=r"tests/integrations/dbapi/test_snowflake\.py:\d+ in work")
# A connection opened by connect() below.
CONNECTED = IsStr(regex=r"tests/integrations/dbapi/test_snowflake\.py:\d+ in connect")

pytestmark = [
    # 5.x asks for one of its logout settings to be set explicitly, and warns.
    pytest.mark.filterwarnings("ignore:enable_server_session_keep_alive_auto_detection:FutureWarning"),
    # A connection may be left for the garbage collector by a test that fails.
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
]


@pytest.fixture(scope="module")
def port() -> Iterator[int]:
    with snowflakestub.serving() as port:
        yield port


@pytest.fixture(autouse=True)
def asked() -> list[str]:
    """The paths the stand-in was asked for, in the test."""
    snowflakestub.Handler.paths.clear()
    return snowflakestub.Handler.paths


Tree = list[tuple[str, str, "Tree"]]


def tree(trace: Trace, under: str | None = None) -> Tree:
    """(kind, name, children) of the spans under the root (or under `under`), in the order they began."""
    parent = under or trace.spans[0].id
    children = sorted((span for span in trace.spans if span.parent_id == parent), key=lambda span: span.start)
    return [(span.kind, span.name, tree(trace, span.id)) for span in children]


def spans(trace: Trace, kind: str) -> list[Span]:
    return [span for span in trace.spans if span.kind == kind]


def where(port: int) -> dict[str, object]:
    """Where the stand-in's statements run, as their spans say it."""
    return {
        "db.client": "snowflake",
        "db.alias": "snowflake:ANALYTICS",
        "db.vendor": "snowflake",
        "db.system": "snowflake",
        "db.namespace": "ANALYTICS",
        "server.address": "127.0.0.1",
        "server.port": port,
    }


def connect(port: int) -> Any:  # noqa: ANN401 (a Snowflake connection)
    return importlib.import_module("snowflake.connector").connect(**snowflakestub.params(port))


def test_a_new_connection_and_a_statement_show_without_the_connector_s_http_calls(
    port: int, asked: list[str], validator: Draft202012Validator
) -> None:
    def work() -> None:
        connection = connect(port)
        cursor = connection.cursor()
        cursor.execute("SELECT %s", (1,))
        assert cursor.fetchall() == [(1,)]
        connection.close()

    trace = traced(work)

    validator.validate(trace.to_dict())
    assert tree(trace) == [("sql.connect", "connect snowflake", []), ("sql", "SELECT", [])]
    assert asked[:2] == ["/session/v1/login-request", "/queries/v1/query-request"]

    assert spans(trace, "sql.connect")[0].attrs == {**where(port), "code.origin": CONNECTED}

    assert spans(trace, "sql")[0].attrs == IsPartialDict(
        {**where(port), "db.statement": "SELECT %s", "db.params": [1], "code.origin": ORIGIN}
    )


def app_call(port: int) -> dict[str, object]:
    """The attributes of the app's own call to the stand-in's /app/report."""
    return {
        "http.request.method": "POST",
        "url.full": f"http://127.0.0.1:{port}/app/report",
        "url.scheme": "http",
        "server.address": "127.0.0.1",
        "server.port": port,
        "http.response.status_code": 200,
        "code.origin": ORIGIN,
        "w3c.span_id": IsStr(regex=r"[0-9a-f]{16}"),
    }


def test_the_app_s_own_calls_show_beside_the_statements(port: int, asked: list[str]) -> None:
    requests = pytest.importorskip("requests")

    def work() -> None:
        connection = connect(port)
        requests.post(f"http://127.0.0.1:{port}/app/report", timeout=5)
        connection.cursor().execute("SELECT 1")
        connection.close()

    trace = traced(work)

    assert tree(trace) == [
        ("sql.connect", "connect snowflake", []),
        ("http.client", "POST 127.0.0.1/app/report", []),
        ("sql", "SELECT", []),
    ]
    assert asked[:3] == ["/session/v1/login-request", "/app/report", "/queries/v1/query-request"]

    assert spans(trace, "http.client")[0].attrs == app_call(port)


def test_the_connector_s_requests_carry_neither_a_traceparent_nor_a_profile_request(port: int) -> None:
    snowflakestub.Handler.carried.clear()
    with start_trace("http", "GET /", profile="1"):
        connection = connect(port)
        connection.cursor().execute("SELECT 1")
        connection.close()

    carried = snowflakestub.Handler.carried
    assert carried == [(None, None)] * len(carried)
    assert len(carried) >= 2


def test_commit_and_rollback_are_statements(port: int) -> None:
    def work() -> None:
        connection = connect(port)
        connection.commit()
        connection.rollback()
        connection.close()

    trace = traced(work)

    assert tree(trace)[1:] == [("sql", "COMMIT", []), ("sql", "ROLLBACK", [])]
    assert [span.attrs["db.statement"] for span in spans(trace, "sql")] == ["COMMIT", "ROLLBACK"]


def test_a_connection_that_fails_to_open_is_an_error(port: int) -> None:
    connector = importlib.import_module("snowflake.connector")

    def work() -> None:
        # Nothing listens on the stand-in's port once it is closed: a free port of its own, then.
        with snowflakestub.serving() as closed:
            pass
        with pytest.raises(connector.errors.Error):
            connector.connect(**{**snowflakestub.params(closed), "login_timeout": 1})

    trace = traced(work)

    connecting = spans(trace, "sql.connect")
    assert [(span.name, span.status) for span in connecting] == [("connect snowflake", "error")]
    assert connecting[0].attrs == {"db.client": "snowflake", "code.origin": ORIGIN}

    assert spans(trace, "http.client") == []


def test_the_asyncio_connector_shows_the_same(port: int, asked: list[str]) -> None:
    aio = pytest.importorskip("snowflake.connector.aio")

    async def work() -> None:
        connection = await aio.connect(**snowflakestub.params(port))
        cursor = connection.cursor()
        await cursor.execute("SELECT %s", (1,))
        assert await cursor.fetchall() == [(1,)]
        await connection.commit()
        await connection.close()

    trace = traced(lambda: asyncio.run(work()))

    assert tree(trace) == [
        ("sql.connect", "connect snowflake", []),
        ("sql", "SELECT", []),
        ("sql", "COMMIT", []),
    ]
    assert asked[:3] == ["/session/v1/login-request", "/queries/v1/query-request", "/queries/v1/query-request"]

    assert spans(trace, "sql.connect")[0].attrs == {**where(port), "code.origin": ORIGIN}


def test_the_app_s_own_aiohttp_call_shows_while_a_statement_runs(port: int) -> None:
    aio = pytest.importorskip("snowflake.connector.aio")
    aiohttp = pytest.importorskip("aiohttp")

    async def call() -> None:
        async with aiohttp.ClientSession() as session, session.post(f"http://127.0.0.1:{port}/app/report") as answer:
            await answer.read()

    async def work() -> None:
        connection = await aio.connect(**snowflakestub.params(port))
        await asyncio.gather(connection.cursor().execute("SELECT 1"), call())
        await connection.close()

    trace = traced(lambda: asyncio.run(work()))

    assert sorted(tree(trace)) == [
        ("http.client", "POST 127.0.0.1/app/report", []),
        ("sql", "SELECT", []),
        ("sql.connect", "connect snowflake", []),
    ]

    assert spans(trace, "http.client")[0].attrs == {**app_call(port), "code.origin": IsStr(regex=r".+ in call")}


# ---------- through SQLAlchemy ----------


def _dialect() -> type:
    """A dialect of SQLAlchemy's on Snowflake's connector, which asks nothing of the session when it connects.

    snowflake-sqlalchemy works with 5.x and SQLAlchemy 2.1 only: test_snowflake_sqlalchemy_s_dialect runs that.
    """
    from sqlalchemy.engine import URL, Connection  # noqa: PLC0415
    from sqlalchemy.engine.default import DefaultDialect  # noqa: PLC0415
    from sqlalchemy.engine.interfaces import ConnectArgsType, DBAPIModule  # noqa: PLC0415

    class SnowflakeStub(DefaultDialect):
        name = "snowflake"
        driver = "connector"
        default_paramstyle = "pyformat"
        supports_statement_cache = True

        @classmethod
        @override
        def import_dbapi(cls) -> DBAPIModule:
            return cast("DBAPIModule", importlib.import_module("snowflake.connector"))

        @override
        def create_connect_args(self, url: URL) -> ConnectArgsType:
            return [], snowflakestub.params(int(url.port or 0))

        @override
        def _get_server_version_info(self, connection: Connection) -> tuple[int, ...]:
            return (9, 30, 0)

        @override
        def _get_default_schema_name(self, connection: Connection) -> str:
            return snowflakestub.SCHEMA

    return SnowflakeStub


@pytest.fixture
def engine(port: int) -> Iterator[Any]:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy.dialects import registry  # noqa: PLC0415

    registry.impls["snowflake.stub"] = _dialect
    engine = sqlalchemy.create_engine(f"snowflake+stub://analyst@127.0.0.1:{port}/ANALYTICS")
    yield engine
    engine.dispose()


def test_a_request_that_opens_a_pool_s_connection_shows_it_and_the_next_one_does_not(engine: Any, port: int) -> None:  # noqa: ANN401
    from sqlalchemy import text  # noqa: PLC0415

    def work() -> None:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar() == 1
            connection.commit()

    cold, warm = traced(work), traced(work)

    # The connection's ROLLBACK as the pool takes it back: SQLAlchemy's own.
    statements: Tree = [
        ("transaction", "transaction ANALYTICS", [("sql", "SELECT", [])]),
        ("sql", "COMMIT", []),
        ("sql", "ROLLBACK", []),
    ]
    # The dialect rolls back the connection it first connected with: inside the connection's time.
    opened: Tree = [("sql", "ROLLBACK", [])]
    assert tree(cold) == [("sql.connect", "connect snowflake", opened), *statements]
    assert tree(warm) == statements

    assert spans(cold, "sql.connect")[0].attrs == {
        "db.client": "sqlalchemy",
        "db.alias": "ANALYTICS",
        "db.vendor": "snowflake",
        "db.system": "snowflake",
        "db.namespace": "ANALYTICS",
        "server.address": "127.0.0.1",
        "server.port": port,
        "db.connection.pool": "sqlalchemy",
        "code.origin": ORIGIN,
    }
    # The statement is SQLAlchemy's, recorded once: the connector's cursor under it leaves it alone.
    assert [span.attrs["db.client"] for span in spans(warm, "sql")] == ["sqlalchemy", "snowflake", "snowflake"]


def test_snowflake_sqlalchemy_s_dialect(port: int) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    url = pytest.importorskip("snowflake.sqlalchemy").URL
    engine = sqlalchemy.create_engine(
        url(account="acme", user="analyst", password="secret", database="ANALYTICS", schema="PUBLIC"),
        connect_args={"host": "127.0.0.1", "port": port, "protocol": "http", "client_session_keep_alive": False},
    )

    def work() -> None:
        with engine.connect() as connection:
            assert connection.execute(sqlalchemy.text("SELECT 1")).scalar() == 1

    try:
        trace = traced(work)
    finally:
        engine.dispose()

    # The dialect's own questions when it first connects are inside the connection's time.
    assert [(kind, name) for kind, name, _ in tree(trace)] == [
        ("sql.connect", "connect snowflake"),
        ("transaction", "transaction ANALYTICS/PUBLIC"),
        ("sql", "ROLLBACK"),
    ]
    assert spans(trace, "sql.connect")[0].attrs == IsPartialDict(
        {"db.alias": "ANALYTICS/PUBLIC", "db.connection.pool": "sqlalchemy", "server.port": IsInt() | None}
    )
