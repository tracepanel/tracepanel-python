"""Benchmarks of the core: what runs for every statement, span and trace a traced app records."""

import json

import pytest
from pytest_codspeed import BenchmarkFixture

from tracepanel.core import context, frames, sql, w3c
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Log, Span, Trace

STATEMENTS = [
    'SELECT "auth_user"."id", "auth_user"."username" FROM "auth_user" WHERE "auth_user"."id" = %s LIMIT 21',
    'SELECT COUNT(*) AS "__count" FROM "auth_user"',
    'UPDATE "members_member" SET "likes" = ("members_member"."likes" + %s) WHERE "members_member"."id" = %s',
    'INSERT INTO "auth_group" ("name") VALUES (%s) RETURNING "auth_group"."id"',
    """SELECT "auth_group"."id", "auth_group"."name"
         FROM "auth_group"
        INNER JOIN "auth_user_groups" ON ("auth_group"."id" = "auth_user_groups"."group_id")
        WHERE "auth_user_groups"."user_id" IN (%s, %s, %s, %s, %s, %s, %s, %s)""",
]
DATABASE: Attrs = {"db.alias": "default", "db.vendor": "sqlite", "db.system": "sqlite"}


def test_fingerprint(benchmark: BenchmarkFixture) -> None:
    @benchmark
    def run() -> None:
        for statement in STATEMENTS:
            sql.fingerprint(statement)
            sql.span_name(statement)


def test_params_attrs(benchmark: BenchmarkFixture) -> None:
    positional = [1, "alice", 3.5, None, True, "x" * 100] * 10
    named = {f"p{i}": value for i, value in enumerate(positional)}

    @benchmark
    def run() -> None:
        sql.params_attrs(positional)
        sql.params_attrs(named)
        sql.params_hash(positional)


def test_new_id(benchmark: BenchmarkFixture) -> None:
    benchmark(lambda: [new_id() for _ in range(100)])


def test_w3c_traceparent(benchmark: BenchmarkFixture) -> None:
    header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    spans = [new_id() for _ in range(20)]

    @benchmark
    def run() -> None:
        trace = Trace(id=new_id(), kind="http", name="GET /", start=0)
        for span in spans:
            w3c.outgoing(trace, span)
        w3c.incoming(header)


@pytest.mark.parametrize("statements", [10, 200])
def test_record_sql_spans(benchmark: BenchmarkFixture, statements: int) -> None:
    """A trace recording statements the way the database integrations do (sql.run)."""

    def database() -> Attrs:
        return DATABASE

    @benchmark
    def run() -> None:
        with context.start_trace("http", "GET /"), context.start_span("view", "view"):
            for index in range(statements):
                sql.run(lambda: None, STATEMENTS[index % len(STATEMENTS)], (index,), many=False, database=database)


def _big_trace(spans: int) -> Trace:
    trace = Trace(id=new_id(), kind="http", name="GET /n-plus-one/", start=0, attrs={"http.status_code": 200})
    root = new_id()
    trace.spans.append(Span(id=root, parent_id=None, kind="http", name="GET /n-plus-one/", start=0))
    for index in range(spans):
        statement = STATEMENTS[index % len(STATEMENTS)]
        attrs: Attrs = {
            **DATABASE,
            "db.statement": statement,
            "db.fingerprint": sql.fingerprint(statement),
            "db.params_hash": sql.params_hash((index,)),
            "db.params": [index],
            "code.origin": "tests/project/views.py:20 in n_plus_one",
        }
        trace.spans.append(Span(id=new_id(), parent_id=root, kind="sql", name="SELECT t", start=index, attrs=attrs))
        trace.logs.append(Log(span_id=root, time=index, level="INFO", logger="app", message=f"row {index}"))
    return trace


def test_trace_to_json(benchmark: BenchmarkFixture) -> None:
    trace = _big_trace(500)
    benchmark(lambda: json.dumps(trace.to_dict()))


def test_traceback_frames(benchmark: BenchmarkFixture) -> None:
    def nested(depth: int) -> None:
        if depth == 0:
            message = "boom"
            raise ValueError(message)
        nested(depth - 1)

    try:
        nested(10)
    except ValueError as error:
        traceback = error.__traceback__

    benchmark(lambda: frames.from_traceback(traceback, with_locals=True))
