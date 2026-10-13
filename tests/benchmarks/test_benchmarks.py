"""What Tracepanel costs the app, measured by CodSpeed: `pytest tests/benchmarks --codspeed`.

The benchmark group (pytest-codspeed) runs them; without it they are skipped. Without --codspeed each runs once, as a
test. CodSpeed counts the instructions each one runs, so a change that makes tracing cost more shows on its pull request
however noisy the machine.
"""

import json
import sqlite3
from collections.abc import Callable, Generator
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest

pytest.importorskip("pytest_codspeed")

from django.contrib.auth.models import Group, User
from django.test import Client
from pytest_codspeed import BenchmarkFixture

import tracepanel.wsgi
from tests.benchmarks.conftest import Discard
from tracepanel.core import conf, sql
from tracepanel.core.context import start_trace
from tracepanel.core.model import Trace
from tracepanel.storage.transport import complete

ROWS = 2000


def traced(work: Callable[[], object]) -> Callable[[], Trace]:
    def run() -> Trace:
        with start_trace("http", "GET /bench") as trace:
            work()
        return trace

    return run


# ---------- a request through Django, tracepanel's middleware and its collectors ----------


@pytest.mark.parametrize("path", ["/", "/n-plus-one/", "/users-page/", "/duplicates/", "/logs/"])
def test_request(
    benchmark: BenchmarkFixture, client: Client, users: list[User], traces: list[Trace], path: str
) -> None:
    client.get(path)

    response = benchmark(client.get, path)

    assert response.status_code == 200


# ---------- a request through the WSGI middleware, without a framework ----------


def _app(environ: dict[str, Any], start_response: Callable[..., object]) -> list[bytes]:
    del environ
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return [b"ok"]


@pytest.fixture
def wsgi_app() -> Generator[tracepanel.wsgi.WSGIApp, None, None]:
    previous = conf._framework  # noqa: SLF001
    yield tracepanel.wsgi.install(_app, enabled=True)
    conf.use_source(previous)


def test_wsgi_request(benchmark: BenchmarkFixture, wsgi_app: tracepanel.wsgi.WSGIApp, discard: Discard) -> None:
    def start_response(status: str, headers: list[tuple[str, str]], exc_info: object = None) -> None:
        del status, headers, exc_info

    def request() -> bytes:
        environ: dict[str, Any] = {"PATH_INFO": "/orders/5"}
        setup_testing_defaults(environ)
        return b"".join(wsgi_app(environ, start_response))

    assert benchmark(request) == b"ok"
    assert discard.submitted > 0


# ---------- reading many rows: each way the SDK counts them ----------


@pytest.fixture
def groups(db: None) -> None:
    Group.objects.bulk_create([Group(name=f"group {n}") for n in range(ROWS)])


def test_django_loads_model_objects(benchmark: BenchmarkFixture, groups: None) -> None:
    trace = benchmark(traced(lambda: list(Group.objects.all())))

    assert trace.spans[-1].attrs["db.response.returned_rows"] == ROWS


def test_sqlite3_iterates_rows(benchmark: BenchmarkFixture) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)")
    connection.executemany("INSERT INTO item (name) VALUES (?)", [(str(n),) for n in range(ROWS)])

    trace = benchmark(traced(lambda: list(connection.execute("SELECT id, name FROM item"))))

    assert trace.spans[-1].attrs["db.response.returned_rows"] == ROWS


def test_sqlalchemy_iterates_rows(benchmark: BenchmarkFixture) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy.pool import StaticPool  # noqa: PLC0415

    engine = sqlalchemy.create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(
            sqlalchemy.text("INSERT INTO item (name) VALUES (:name)"), [{"name": str(n)} for n in range(ROWS)]
        )

    def read() -> None:
        with engine.connect() as connection:
            list(connection.execute(sqlalchemy.text("SELECT id, name FROM item")))

    trace = benchmark(traced(read))

    assert trace.spans[-1].attrs["db.response.returned_rows"] == ROWS


# ---------- the SDK's own work on a statement and a trace ----------

STATEMENT = (
    'SELECT "auth_user"."id", "auth_user"."username", "auth_user"."email" FROM "auth_user" '
    'INNER JOIN "auth_user_groups" ON ("auth_user"."id" = "auth_user_groups"."user_id") '
    'WHERE ("auth_user_groups"."group_id" = %s AND "auth_user"."is_active") ORDER BY "auth_user"."id" ASC'
)


def test_record_statements(benchmark: BenchmarkFixture) -> None:
    def record() -> Trace:
        with start_trace("http", "GET /bench") as trace:
            for group in range(100):
                sql.record(STATEMENT, (group,), many=False, database=dict, start=0, duration=100, status="ok")
        return trace

    trace = benchmark(record)

    assert len([span for span in trace.spans if span.kind == "sql"]) == 100


def test_fingerprint_statements(benchmark: BenchmarkFixture) -> None:
    # Prefetching: one statement, an IN list as long as the objects it prefetches for.
    statements = [STATEMENT.replace("= %s", f"IN ({', '.join(['%s'] * n)})") for n in range(1, 201)]

    fingerprints = benchmark(lambda: {sql.fingerprint(statement) for statement in statements})

    assert len(fingerprints) == 1


def test_a_trace_recorded_completed_and_encoded(benchmark: BenchmarkFixture) -> None:
    def build() -> Trace:
        with start_trace("http", "GET /bench") as trace:
            for group in range(200):
                sql.record(STATEMENT, (group,), many=False, database=dict, start=0, duration=100, status="ok")
        return trace

    # What happens to every trace after its response: completed, then encoded for the agent.
    encoded = benchmark(lambda: json.dumps([complete(build()).to_dict()], separators=(",", ":")))

    assert encoded.startswith('[{"')
