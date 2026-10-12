"""Who called the driver decides who records the statement: the app here, an ORM itself, never both."""

import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from tests.integrations.dbapi.conftest import sql_spans, traced
from tracepanel.integrations import sqlalchemy as sqlalchemy_integration


def calling_from(module: str) -> Callable[[sqlite3.Connection, str], Any]:
    """A function that runs a statement, defined as if in that module (the stack shows the module's name)."""
    namespace: dict[str, Any] = {"__name__": module}
    exec("def run(connection, statement):\n    return connection.execute(statement)\n", namespace)  # noqa: S102
    return namespace["run"]


@pytest.mark.parametrize("module", ["django.db.backends.utils", "sqlalchemy.engine.default", "tracepanel.core.explain"])
def test_a_layer_that_records_its_own_statements_keeps_them(module: str) -> None:
    connection = sqlite3.connect(":memory:")

    trace = traced(lambda: calling_from(module)(connection, "SELECT 1"))

    assert sql_spans(trace) == []


@pytest.mark.parametrize("module", ["peewee", "app.repository", "databases.core"])
def test_the_app_or_a_library_on_the_driver_gets_them_recorded(module: str) -> None:
    connection = sqlite3.connect(":memory:")

    trace = traced(lambda: calling_from(module)(connection, "SELECT 1"))

    assert sql_spans(trace) == [("sqlite3", "SELECT")]


def test_the_driver_s_own_frames_are_passed_over() -> None:
    connection = sqlite3.connect(":memory:")
    # As psycopg's Connection.execute calls its cursor's: the driver's frame, then the app's.
    driver = calling_from("pymysql.cursors")

    def app() -> None:
        driver(connection, "SELECT 1")

    assert sql_spans(traced(app)) == [("sqlite3", "SELECT")]


def test_sqlalchemy_on_sqlite3_is_recorded_by_sqlalchemy_only() -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite://")

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))
            connection.exec_driver_sql("SELECT 2")

    trace = traced(work)

    assert sql_spans(trace) == [("sqlalchemy", "SELECT"), ("sqlalchemy", "SELECT")]
    # The engine's connection is the traced driver's: the check, not a missing wrapper, kept the statements out.
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
    from django.contrib.auth.models import User  # noqa: PLC0415
    from django.db import connections  # noqa: PLC0415

    # A connection opened now, through the wrapped driver (the test's own may predate it).
    connection = connections.create_connection("default")

    def work() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        list(User.objects.all())

    try:
        trace = traced(work)

        assert [client for client, _ in sql_spans(trace)] == ["django", "django"]
        if connection.vendor == "sqlite":
            assert type(connection.connection).__name__.startswith("Traced")
    finally:
        connection.close()
