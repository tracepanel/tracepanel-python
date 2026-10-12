"""The drivers' tests, and the servers they run on.

The test project's Django app installs every integration at startup, these included: every Django and SQLAlchemy test
runs on the wrapped drivers, and would see a statement recorded twice. Installed here too (a no-op then) for these
modules run alone.

The servers come from the environment: TRACEPANEL_TEST_DB=postgres (PG* variables), and
TRACEPANEL_TEST_MYSQL / TRACEPANEL_TEST_ORACLE for the MySQL and Oracle drivers (MYSQL_* / ORACLE_* variables).
"""

import os
from collections.abc import Callable

import pytest

from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.integrations import dbapi
from tracepanel.integrations import sqlalchemy as sqlalchemy_integration
from tracepanel.storage.transport import complete

dbapi.install()
sqlalchemy_integration.install()

postgres = pytest.mark.skipif(os.environ.get("TRACEPANEL_TEST_DB") != "postgres", reason="needs PostgreSQL")
mysql = pytest.mark.skipif(not os.environ.get("TRACEPANEL_TEST_MYSQL"), reason="needs MySQL")
oracle = pytest.mark.skipif(not os.environ.get("TRACEPANEL_TEST_ORACLE"), reason="needs Oracle")


def traced(work: Callable[[], object]) -> Trace:
    with start_trace("http", "GET /") as trace:
        work()
    return complete(trace)


def statements(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "sql"]


def sql_spans(trace: Trace) -> list[tuple[str, str]]:
    """(db.client, span name) of each statement, in order."""
    return [(str(span.attrs["db.client"]), span.name) for span in statements(trace)]


def pg() -> dict[str, str]:
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": os.environ.get("PGPORT", "5432"),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", "postgres"),
        "dbname": os.environ.get("PGDATABASE", "tracepanel"),
    }


def pg_url(dialect: str) -> str:
    p = pg()
    return f"postgresql+{dialect}://{p['user']}:{p['password']}@{p['host']}:{p['port']}/{p['dbname']}"


def my() -> dict[str, str | int]:
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MYSQL_PORT", "3306")),
        "user": os.environ.get("MYSQL_USER", "root"),
        "password": os.environ.get("MYSQL_PASSWORD", "mysql"),
        "database": os.environ.get("MYSQL_DATABASE", "tracepanel"),
    }


def my_url(dialect: str) -> str:
    m = my()
    return f"mysql+{dialect}://{m['user']}:{m['password']}@{m['host']}:{m['port']}/{m['database']}"
