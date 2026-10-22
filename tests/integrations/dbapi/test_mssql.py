from collections.abc import Callable, Generator
from typing import Any

import pytest
from dirty_equals import IsPartialDict, IsStr

from tests.integrations.dbapi.conftest import ms, ms_odbc, mssql, sql_spans, statements, traced
from tracepanel.core import explain as plans
from tracepanel.integrations import dbapi
from tracepanel.integrations import sqlalchemy as sqlalchemy_integration
from tracepanel.integrations.dbapi import mssql as mssql_driver

ORIGIN = IsStr(regex=r"tests/integrations/dbapi/test_mssql\.py:\d+ in work")


def pyodbc_connect() -> Any:  # noqa: ANN401
    return pytest.importorskip("pyodbc").connect(ms_odbc(), autocommit=True)


def pymssql_connect() -> Any:  # noqa: ANN401
    m = ms()
    return pytest.importorskip("pymssql").connect(
        server=m["host"],
        port=m["port"],
        user=m["user"],
        password=m["password"],
        database=m["database"],
        autocommit=True,
    )


def sql(statement: str, mark: str) -> str:
    """The statement with the driver's placeholder for %s."""
    return statement.replace("%s", mark)


# Each driver, and its placeholder.
DRIVERS: dict[str, tuple[Callable[[], Any], str]] = {
    "pyodbc": (pyodbc_connect, "?"),
    "pymssql": (pymssql_connect, "%s"),
}


@pytest.fixture(scope="module")
def orders() -> Generator[None, None, None]:
    """The test database, and a table with no index (a heap): its plans scan it."""
    connection = connect_any("master")
    cursor = connection.cursor()
    cursor.execute(f"IF DB_ID('{ms()['database']}') IS NULL CREATE DATABASE {ms()['database']}")
    connection.close()
    connection = connect_any(ms()["database"])
    cursor = connection.cursor()
    cursor.execute("DROP TABLE IF EXISTS tp_orders")
    cursor.execute("CREATE TABLE tp_orders (id INT NOT NULL, customer INT NOT NULL, total INT NOT NULL)")
    cursor.execute("INSERT INTO tp_orders VALUES (1, 7, 10), (2, 7, 20), (3, 8, 30)")
    connection.close()
    yield
    connection = connect_any(ms()["database"])
    connection.cursor().execute("DROP TABLE IF EXISTS tp_orders")
    connection.close()


def connect_any(database: str) -> Any:  # noqa: ANN401
    """A connection with whichever driver is installed, for the tests' own setup."""
    m = ms()
    try:
        import pymssql  # noqa: PLC0415
    except ImportError:
        import pyodbc  # noqa: PLC0415

        return pyodbc.connect(ms_odbc(database), autocommit=True)
    return pymssql.connect(
        server=m["host"], port=m["port"], user=m["user"], password=m["password"], database=database, autocommit=True
    )


# ---------- on a server ----------


@mssql
@pytest.mark.usefixtures("orders")
@pytest.mark.parametrize("driver", DRIVERS)
def test_statements_are_recorded_once_each(driver: str) -> None:
    connect, mark = DRIVERS[driver]
    connection = connect()

    def work() -> None:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT total FROM tp_orders WHERE customer = %s", mark), (7,))
        cursor.fetchall()
        cursor.executemany(sql("UPDATE tp_orders SET total = total WHERE id = %s", mark), [(1,), (2,)])
        cursor.close()

    try:
        trace = traced(work)
    finally:
        connection.close()

    assert sql_spans(trace) == [(driver, "SELECT tp_orders"), (driver, "UPDATE tp_orders")]
    assert [span.attrs["db.many"] for span in statements(trace)] == [False, True]
    assert statements(trace)[0].attrs == IsPartialDict(
        {
            "db.client": driver,
            "db.alias": f"{driver}:{ms()['database']}",
            "db.vendor": "mssql",
            "db.system": "mssql",
            "db.namespace": ms()["database"],
            "server.address": ms()["host"],
            "server.port": int(ms()["port"]),
            "db.statement": sql("SELECT total FROM tp_orders WHERE customer = %s", mark),
            "db.params": [7],
            "db.response.returned_rows": 2,
            "code.origin": ORIGIN,
        }
    )


@mssql
@pytest.mark.usefixtures("orders")
@pytest.mark.parametrize("driver", DRIVERS)
def test_the_rows_fetched_are_counted(driver: str) -> None:
    connect, mark = DRIVERS[driver]
    connection = connect()

    def work() -> None:
        with connection.cursor() as cursor:
            cursor.execute(sql("SELECT id FROM tp_orders WHERE customer = %s", mark), (7,))
            list(cursor)
            cursor.execute("SELECT id FROM tp_orders ORDER BY id")
            cursor.fetchone()
            cursor.fetchmany(1)
            cursor.execute(sql("SELECT id FROM tp_orders WHERE customer IN (%s, %s)", mark), (7, 8))
            cursor.fetchall()

    try:
        trace = traced(work)
    finally:
        connection.close()

    assert [span.attrs.get("db.response.returned_rows") for span in statements(trace)] == [2, 2, 3]
    assert statements(trace)[2].attrs["db.fingerprint"] == "SELECT id FROM tp_orders WHERE customer IN (...)"


@mssql
@pytest.mark.usefixtures("orders")
def test_pyodbc_shortcuts_and_values_one_by_one() -> None:
    connection = pyodbc_connect()

    def work() -> None:
        # Connection.execute: a new cursor's. Values passed one by one, and the cursor execute returns.
        rows = connection.execute("SELECT id FROM tp_orders WHERE customer = ? AND total > ?", 7, 15).fetchall()
        assert [tuple(row) for row in rows] == [(2,)]
        cursor = connection.cursor()
        assert cursor.execute("SELECT COUNT(*) FROM tp_orders WHERE customer = ?", 8).fetchval() == 1
        cursor.fast_executemany = True
        assert cursor.fast_executemany is True

    try:
        trace = traced(work)
    finally:
        connection.close()

    assert sql_spans(trace) == [("pyodbc", "SELECT tp_orders"), ("pyodbc", "SELECT COUNT tp_orders")]
    assert [span.attrs["db.params"] for span in statements(trace)] == [[7, 15], [8]]


@mssql
@pytest.mark.usefixtures("orders")
@pytest.mark.parametrize("driver", DRIVERS)
def test_a_statement_can_be_explained(driver: str) -> None:
    connect, mark = DRIVERS[driver]
    connection = connect()
    statement = sql("SELECT total FROM tp_orders WHERE customer = %s", mark)
    try:
        trace = traced(lambda: connection.cursor().execute(statement, (7,)))
    finally:
        connection.close()
    alias = str(statements(trace)[0].attrs["db.alias"])

    plan = dbapi.explain(statement, [7], alias)

    assert plan.to_dict() == {
        "vendor": "mssql",
        "alias": alias,
        "analyzed": False,
        "lines": [
            {
                "depth": 0,
                "text": IsStr(
                    regex=r"^Table Scan\(OBJECT:\(\[tracepanel\]\.\[dbo\]\.\[tp_orders\]\).*\(rows=.* cost=.*\)$"
                ),
                "warning": "Full scan of tp_orders: no index is used",
            }
        ],
    }


@mssql
@pytest.mark.usefixtures("orders")
@pytest.mark.parametrize("dialect", ["pyodbc", "pymssql"])
def test_sqlalchemy_on_a_sql_server_driver_is_recorded_by_sqlalchemy_only(dialect: str) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip(dialect)
    m = ms()
    query = "?driver=" + m["driver"].replace(" ", "+") + "&TrustServerCertificate=yes" if dialect == "pyodbc" else ""
    url = f"mssql+{dialect}://{m['user']}:{m['password']}@{m['host']}:{m['port']}/{m['database']}{query}"
    engine = sqlalchemy.create_engine(url)
    statement = sqlalchemy.text("SELECT total FROM tp_orders WHERE customer = :customer")

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(statement, {"customer": 7}).fetchall()

    try:
        trace = traced(work)
        span = statements(trace)[0]
        plan = sqlalchemy_integration.explain(
            str(span.attrs["db.statement"]), plans.params_from(span.attrs), str(span.attrs["db.alias"])
        )
    finally:
        engine.dispose()

    assert sql_spans(trace) == [("sqlalchemy", "SELECT tp_orders")]
    assert span.attrs == IsPartialDict({"db.vendor": "mssql", "db.system": "mssql"})
    assert [line.warning for line in plan.lines] == ["Full scan of tp_orders: no index is used"]


# ---------- without a server ----------


def test_explain_runs_the_statement_in_a_showplan_session() -> None:
    ran: list[tuple[str, plans.Values, plans.Session]] = []
    columns = ["StmtText", "NodeId", "Parent", "PhysicalOp", "EstimateRows", "TotalSubtreeCost", "Warnings", "Type"]
    statement = "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer WHERE o.total > %s"
    rows: plans.Rows = [
        (statement.replace("%s", "10"), 1, 0, None, 4.0, 0.0331, None, "SELECT"),
        ("  |--Nested Loops(Inner Join)", 2, 1, "Nested Loops", 4.0, 0.0331, "NO JOIN PREDICATE", "PLAN_ROW"),
        (
            "       |--Clustered Index Scan(OBJECT:([shop].[dbo].[orders].[PK_orders] AS [o]))",
            3,
            2,
            "Clustered Index Scan",
            4.0,
            0.0032,
            None,
            "PLAN_ROW",
        ),
        (
            "       |--Index Seek(OBJECT:([shop].[dbo].[customers].[ix_id] AS [c]))",
            4,
            2,
            "Index Seek",
            1.0,
            0.0032,
            None,
            "PLAN_ROW",
        ),
    ]

    def execute(sql: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
        ran.append((sql, values, session))
        return rows, columns

    # Django's vendor name for SQL Server (mssql-django): the same plan.
    plan = plans.explain(statement, [10], vendor="microsoft", alias="default", execute=execute, analyze=True)

    # The values written in: a statement sent with parameters shows no plan (pyodbc's).
    inlined = statement.replace("%s", "10")
    assert ran == [(inlined, None, ("SET SHOWPLAN_ALL ON", "SET SHOWPLAN_ALL OFF"))]
    assert plan.to_dict() == {
        "vendor": "microsoft",
        "alias": "default",
        "analyzed": False,
        "lines": [
            {
                "depth": 0,
                "text": "Nested Loops(Inner Join)  (rows=4 cost=0.0331)",
                "warning": "SQL Server warns: NO JOIN PREDICATE",
            },
            {
                "depth": 1,
                "text": "Clustered Index Scan(OBJECT:([shop].[dbo].[orders].[PK_orders] AS [o]))  (rows=4 cost=0.0032)",
                "warning": "Full scan of orders: no index is used",
            },
            {
                "depth": 1,
                "text": "Index Seek(OBJECT:([shop].[dbo].[customers].[ix_id] AS [c]))  (rows=1 cost=0.0032)",
                "warning": None,
            },
        ],
    }


@pytest.mark.parametrize(
    ("statement", "params", "inlined"),
    [
        (
            "SELECT * FROM t WHERE a = %s AND b LIKE '%%x?' AND c = %s -- %s",
            ["it's", None],
            "SELECT * FROM t WHERE a = N'it''s' AND b LIKE '%x?' AND c = NULL -- %s",
        ),
        (
            "SELECT * FROM t WHERE a = ? AND b = ? AND c LIKE '%%'",
            [True, 2.5],
            "SELECT * FROM t WHERE a = 1 AND b = 2.5 AND c LIKE '%%'",
        ),
        ("SELECT * FROM t WHERE a = %(a)s OR b = %(a)s", {"a": 7}, "SELECT * FROM t WHERE a = 7 OR b = 7"),
        ("SELECT 1", None, "SELECT 1"),
    ],
)
def test_parameters_are_written_into_the_statement_as_literals(
    statement: str, params: plans.Values, inlined: str
) -> None:
    ran: list[str] = []

    def execute(sql: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
        ran.append(sql)
        return [], []

    plans.explain(statement, params, vendor="mssql", alias="default", execute=execute)

    assert ran == [inlined]


def test_parameters_that_do_not_match_the_placeholders_are_refused() -> None:
    with pytest.raises(plans.ExplainError, match="don't match the query's placeholders"):
        plans.explain("SELECT ? + ?", [1], vendor="mssql", alias="default", execute=lambda *_: ([], []))


def test_the_session_is_set_back_when_the_statement_fails() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.ran: list[str] = []

        def execute(self, sql: str, values: object = None) -> None:
            self.ran.append(sql)
            if sql.startswith("SELECT"):
                msg = "Invalid object name 'missing'."
                raise RuntimeError(msg)

    cursor = Cursor()

    with pytest.raises(RuntimeError, match="Invalid object name"):
        plans.cursor_rows(cursor, "SELECT * FROM missing", None, plans.MSSQL_SESSION)

    assert cursor.ran == ["SET SHOWPLAN_ALL ON", "SELECT * FROM missing", "SET SHOWPLAN_ALL OFF"]


@pytest.mark.parametrize(
    ("args", "kwargs", "where"),
    [
        (
            ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=tcp:db.local,14330;DATABASE=shop;UID=app;PWD={p;w}",),
            {"autocommit": True},
            ("db.local", "14330", "shop"),
        ),
        (
            ("Driver={ODBC Driver 18 for SQL Server}",),
            {"host": "db.local", "database": "shop"},
            ("db.local", "", "shop"),
        ),
        (("DSN=shop",), {}, ("", "", "")),
    ],
)
def test_where_a_pyodbc_connection_string_points(
    args: tuple[str, ...], kwargs: dict[str, object], where: tuple[str, str, str]
) -> None:
    pytest.importorskip("pyodbc")

    found = mssql_driver._pyodbc_where(args, kwargs)  # noqa: SLF001

    assert (found.client, found.vendor, found.host, found.port, found.name) == ("pyodbc", "mssql", *where)


@pytest.mark.parametrize(
    ("args", "kwargs", "where"),
    [
        ((), {"server": "db.local", "port": "14330", "database": "shop"}, ("db.local", "14330", "shop")),
        (("db.local:14330", "app", "pw", "shop"), {}, ("db.local", "14330", "shop")),
        ((), {"server": r"db.local\SQLEXPRESS"}, (r"db.local\SQLEXPRESS", "", "")),
    ],
)
def test_where_a_pymssql_connection_points(
    args: tuple[str, ...], kwargs: dict[str, object], where: tuple[str, str, str]
) -> None:
    pytest.importorskip("pymssql")

    found = mssql_driver._pymssql_where(args, kwargs)  # noqa: SLF001

    assert (found.client, found.vendor, found.host, found.port, found.name) == ("pymssql", "mssql", *where)
