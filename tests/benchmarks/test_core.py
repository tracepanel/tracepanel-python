"""The core's hot paths: what runs for every statement, span and error a traced app records."""

import json

import pytest

pytest.importorskip("pytest_codspeed")

from pytest_codspeed import BenchmarkFixture

from tracepanel.core import frames, ids, sql, w3c
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Log, Span, Trace

STATEMENTS = [
    'SELECT "auth_user"."id", "auth_user"."username" FROM "auth_user" WHERE "auth_user"."id" = %s LIMIT 21',
    'SELECT COUNT(*) AS "__count" FROM "auth_user"',
    'UPDATE "members_member" SET "likes" = ("members_member"."likes" + %s) WHERE "members_member"."id" = %s',
    'INSERT INTO "auth_group" ("name") VALUES (%s) RETURNING "auth_group"."id"',
]
DATABASE: Attrs = {"db.alias": "default", "db.vendor": "sqlite", "db.system": "sqlite"}


def test_span_names(benchmark: BenchmarkFixture) -> None:
    names = benchmark(lambda: [sql.span_name(statement) for statement in STATEMENTS])

    assert names == ["SELECT auth_user", "SELECT COUNT auth_user", "UPDATE members_member", "INSERT auth_group"]


def test_parameters(benchmark: BenchmarkFixture) -> None:
    positional = [1, "alice", 3.5, None, True, "x" * 100] * 10
    named = {f"p{index}": value for index, value in enumerate(positional)}

    def run() -> str:
        sql.params_attrs(positional)
        sql.params_attrs(named)
        return sql.params_hash(positional)

    assert benchmark(run) == sql.params_hash(positional)


def test_ids(benchmark: BenchmarkFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    # One millisecond throughout: how many the run spans would change what it measures (the time part is cached for
    # one), and the simulation runs far slower than the clock.
    monkeypatch.setattr(ids.time, "time_ns", lambda: 1_791_878_400_000_000_000)

    ids_made = benchmark(lambda: [new_id() for _ in range(100)])

    assert len(set(ids_made)) == 100


def test_w3c_traceparent(benchmark: BenchmarkFixture) -> None:
    header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    spans = [new_id() for _ in range(20)]

    def run() -> Attrs:
        trace = Trace(id=new_id(), kind="http", name="GET /", start=0)
        for span in spans:
            w3c.outgoing(trace, span)
        return w3c.incoming(header)

    assert benchmark(run)["w3c.trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


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


def test_a_trace_of_500_spans_and_logs_to_json(benchmark: BenchmarkFixture) -> None:
    trace = _big_trace(500)

    encoded = benchmark(lambda: json.dumps(trace.to_dict(), separators=(",", ":")))

    assert len(json.loads(encoded)["spans"]) == 501


def test_an_error_s_frames_with_their_locals(benchmark: BenchmarkFixture) -> None:
    def nested(depth: int) -> None:
        if depth == 0:
            message = "boom"
            raise ValueError(message)
        nested(depth - 1)

    try:
        nested(10)
    except ValueError as error:
        traceback = error.__traceback__

    found = benchmark(lambda: frames.from_traceback(traceback, with_locals=True))

    assert len(found) == 12
