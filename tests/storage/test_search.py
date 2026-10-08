import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dirty_equals import IsInt, IsList, IsPartialDict, IsPositiveFloat, IsPositiveInt, IsStr
from django.contrib.auth.models import User
from django.test import Client

from tracepanel.storage import search
from tracepanel.storage.search import SearchError, Term, parse
from tracepanel.storage.store import TRACE_SEARCH, Store
from tracepanel.storage.transport import StoreWriter, set_writer

SPEC = Path(__file__).resolve().parents[2] / "schema" / "search.json"


def test_parse_free_text_and_filters() -> None:
    assert parse('route:/api/* status>=500 "connection refused" slow') == [
        Term("route", ":", "/api/*"),
        Term("status", ">=", "500"),
        Term(None, ":", "connection refused"),
        Term(None, ":", "slow"),
    ]


def test_durations() -> None:
    assert search.parse_duration_us("200ms") == 200_000
    assert search.parse_duration_us("1.5s") == 1_500_000
    assert search.parse_duration_us("300") == 300_000
    with pytest.raises(ValueError, match="duration"):
        search.parse_duration_us("fast")


def test_unknown_filter_and_bad_values_are_explained() -> None:
    with pytest.raises(SearchError, match="Unknown filter 'colour'"):
        search.compile_where("colour:red", TRACE_SEARCH)
    with pytest.raises(SearchError, match="status expects a number"):
        search.compile_where("status>=lots", TRACE_SEARCH)
    with pytest.raises(SearchError, match="has expects"):
        search.compile_where("has:bugs", TRACE_SEARCH)


def test_spec_file_matches_the_code() -> None:
    # schema/search.json is the portable contract the cloud implements too.
    assert json.loads(SPEC.read_text()) == search.spec()


@pytest.fixture
def recorded(client: Client, users: list[User], store: Store) -> Store:
    """A few real requests written to the store."""
    store.max_traces = 100
    writer = StoreWriter(store)
    set_writer(writer)
    client.raise_request_exception = False
    try:
        for path in ("/n-plus-one/", "/fixed/", "/duplicates/", "/logs/", "/error/", "/cache/"):
            client.get(path)
        writer.flush()
    finally:
        set_writer(None)
    return store


def names(store: Store, query: str) -> list[str]:
    items, _ = store.search_traces(query)
    return sorted(str(item["path"]) for item in items)


@pytest.mark.django_db
def test_trace_filters(recorded: Store) -> None:
    assert names(recorded, "status>=500") == ["/error/"]
    assert names(recorded, "has:n+1") == ["/n-plus-one/"]
    assert names(recorded, "has:duplicates") == ["/duplicates/"]
    assert names(recorded, "has:logs route:/logs/") == ["/logs/"]
    assert names(recorded, "path:/n-* ") == ["/n-plus-one/"]
    assert names(recorded, "method:get queries>5 has:issues") == ["/n-plus-one/"]
    assert "/fixed/" in names(recorded, "duration<10s status:200")
    assert names(recorded, "plus") == ["/n-plus-one/"]


@pytest.mark.django_db
def test_trace_pagination(recorded: Store) -> None:
    first, cursor = recorded.search_traces("", limit=4)
    assert len(first) == 4
    assert cursor is not None
    rest, end = recorded.search_traces("", limit=4, before=cursor)
    assert end is None
    assert {str(item["id"]) for item in first}.isdisjoint(str(item["id"]) for item in rest)
    assert len(first) + len(rest) == 6


@pytest.mark.django_db
def test_log_search(recorded: Store) -> None:
    logs, _ = recorded.search_logs("hello")
    assert [log["message"] for log in logs] == ["hello world"]
    assert logs[0]["path"] == "/logs/"
    errors, _ = recorded.search_logs("level>=error")
    assert {log["message"] for log in errors} >= {"parse failed"}
    assert recorded.search_logs("logger:tests.* careful")[0][0]["level"] == "warning"


@pytest.mark.django_db
def test_query_and_endpoint_aggregates(recorded: Store) -> None:
    # The N+1 statement: the one called once per user (ties in total time make list order unreliable).
    n_plus_one = max(recorded.list_queries("table:auth_group"), key=lambda query: int(str(query["calls"])))
    assert n_plus_one == IsPartialDict(calls=5, most_per_trace=5, origin=IsStr(regex=r"tests/project/views\.py:.*"))
    fp = str(n_plus_one["fp"])
    assert names(recorded, f"query:{fp}") == ["/n-plus-one/"]

    endpoints = {str(e["route"]): e for e in recorded.list_endpoints()}
    assert endpoints["/error/"]["errors"] == 1
    assert endpoints["/n-plus-one/"]["with_issues"] == 1
    assert endpoints["/n-plus-one/"]["calls"] == 1


@pytest.mark.django_db
def test_api(client: Client, recorded: Store) -> None:
    body = client.get("/__tracepanel__/api/traces", {"q": "has:n+1"}).json()
    assert [t["path"] for t in body["traces"]] == ["/n-plus-one/"]
    assert body["next"] is None
    assert client.get("/__tracepanel__/api/traces", {"q": "nope:1"}).status_code == 400
    assert client.get("/__tracepanel__/api/logs", {"q": "careful"}).json()["logs"][0]["message"] == "careful with fire"
    assert client.get("/__tracepanel__/api/queries").json()["queries"]
    assert client.get("/__tracepanel__/api/endpoints").json()["endpoints"]
    assert client.get("/__tracepanel__/api/issues", {"q": "kind:error"}).json()["issues"][0]["kind"] == "error"
    assert client.get("/__tracepanel__/api/search").json()["traces"]["status"]["type"] == "number"


@pytest.mark.django_db
def test_endpoint_detail(client: Client, recorded: Store) -> None:
    detail = recorded.endpoint_detail("get", "/n-plus-one/")
    assert detail is not None
    summary = detail["summary"]
    assert isinstance(summary, dict)
    assert summary["calls"] == 1
    assert summary["p50"] == summary["p99"] == summary["max"]
    assert summary["avg_db_time"] > 0
    series = detail["series"]
    assert isinstance(series, list)
    assert sum(int(bucket["calls"]) for bucket in series) == 1
    issues = detail["issues"]
    assert isinstance(issues, list)
    assert {issue["kind"] for issue in issues} == {"n+1"}
    assert recorded.endpoint_detail("GET", "/nope/") is None
    body = client.get("/__tracepanel__/api/endpoints/detail", {"method": "GET", "route": "/n-plus-one/"}).json()
    assert body["route"] == "/n-plus-one/"
    assert sum(bucket["count"] for bucket in body["histogram"]) == 1


def test_traces_overview(client: Client, recorded: Store) -> None:
    operation = {
        "kind": "http",
        "name": IsStr(regex=r"GET /.*"),
        "method": "GET",
        "route": IsStr,
        "calls": 1,
        "errors": IsInt,
        "p95": IsPositiveInt,
        "slow": 0,
    }

    assert recorded.traces_overview() == {
        "summary": {
            "calls": 6,
            "errors": 1,
            "slow": 0,
            "p50": IsPositiveInt,
            "p75": IsPositiveInt,
            "p90": IsPositiveInt,
            "p95": IsPositiveInt,
            "p99": IsPositiveInt,
            "max": IsPositiveInt,
            "avg_queries": IsPositiveFloat,
            "avg_db_time": IsPositiveInt,
            "with_issues": 3,
            "first_seen": IsPositiveInt,
            "last_seen": IsPositiveInt,
        },
        "series": IsList(length=60),
        "histogram": IsList(length=(1, ...)),
        "slow_threshold": 500_000,
        "kinds": [{"kind": "http", "calls": 6, "errors": 1, "p95": IsPositiveInt, "slow": 0}],
        "slowest": IsList(*[operation] * 6),
        "failing": [operation | {"name": "GET /error/", "route": "/error/", "errors": 1}],
    }
    assert recorded.traces_overview("route:/n-plus-one/")["summary"] == IsPartialDict(calls=1, errors=0)
    assert recorded.traces_overview("route:/nope/") == {
        "summary": None,
        "series": IsList,
        "histogram": [],
        "slow_threshold": 500_000,
        "kinds": [],
        "slowest": [],
        "failing": [],
    }
    assert client.get("/__tracepanel__/api/traces/overview", {"q": "status>=500"}).json()["summary"] == IsPartialDict(
        calls=1, errors=1
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90s", 1_000_000_000_000 - 90_000_000),
        ("15m", 1_000_000_000_000 - 900_000_000),
        ("2h", 1_000_000_000_000 - 7_200_000_000),
        ("1d", 1_000_000_000_000 - 86_400_000_000),
        ("2026-10-07T13:30:00+00:00", 1_791_379_800_000_000),
    ],
)
def test_parse_time(value: str, expected: int) -> None:
    assert search.parse_time_us(value, now_us=1_000_000_000_000) == expected


def test_time_filters(recorded: Store) -> None:
    items, _ = recorded.search_traces()
    middle = sorted(int(str(item["start"])) for item in items)[3]
    moment = datetime.fromtimestamp(middle / 1_000_000, tz=UTC).isoformat()

    assert [len(recorded.search_traces(query)[0]) for query in ("since:1h", "until:1h")] == [6, 0]
    assert len(recorded.search_traces(f"since:{moment}")[0]) == 3
    assert len(recorded.search_traces(f"until:{moment}")[0]) == 3
    assert len(recorded.search_logs("since:1h")[0]) == len(recorded.search_logs()[0])


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("since>5m", "since supports only :, like since:15m"),
        ("until:yesterday", "until expects a time like 15m, 2h, 1d or 2026-10-07T13:30, got 'yesterday'"),
    ],
)
def test_time_filter_errors(recorded: Store, query: str, message: str) -> None:
    with pytest.raises(SearchError, match=re.escape(message)):
        recorded.search_traces(query)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("path:/fixed/,/logs/", ["/fixed/", "/logs/"]),
        ("path:/n-*,/dup*", ["/duplicates/", "/n-plus-one/"]),
        ("path!=/fixed/,/logs/ status<500", ["/cache/", "/duplicates/", "/n-plus-one/"]),
        ("status:200,500 has:issues", ["/duplicates/", "/error/", "/n-plus-one/"]),
        ("method:get,post route:/error/", ["/error/"]),
    ],
)
def test_any_of_values(recorded: Store, query: str, expected: list[str]) -> None:
    assert names(recorded, query) == expected


@pytest.mark.django_db
def test_any_of_levels(recorded: Store) -> None:
    logs, _ = recorded.search_logs("level:warning,error")

    assert sorted((log["level"], log["message"]) for log in logs) == [
        ("error", "Internal Server Error: /error/"),
        ("error", "parse failed"),
        ("warning", "careful with fire"),
    ]

    with pytest.raises(SearchError, match=re.escape("level expects a level")):
        recorded.search_logs("level:warning,loud")


@pytest.mark.django_db
def test_known_values(client: Client, recorded: Store) -> None:
    no_tasks: list[str] = []
    expected = {
        "route": ["/cache/", "/duplicates/", "/error/", "/fixed/", "/logs/", "/n-plus-one/"],
        "logger": IsList("django.request", "tests.project.views", check_order=False),
        "db": ["default"],
        "task": no_tasks,
        "queue": no_tasks,
        # django.request adds request and status_code to its own records.
        "extra": ["extra.order_id", "extra.payment_token", "extra.request", "extra.status_code", "extra.tags"],
    }

    assert recorded.known_values() == expected
    assert client.get("/__tracepanel__/api/values").json() == expected


@pytest.mark.django_db
def test_overview_spans_the_searched_window(recorded: Store) -> None:
    assert recorded.traces_overview("since:2h until:1h", buckets=4) == {
        "summary": None,
        "series": [IsPartialDict(calls=0, errors=0, p50=None, p95=None)] * 4,
        "histogram": [],
        "slow_threshold": 500_000,
        "kinds": [],
        "slowest": [],
        "failing": [],
    }


@pytest.mark.django_db
def test_issues(client: Client, recorded: Store) -> None:
    error = {
        "fingerprint": IsStr,
        "fingerprint_version": 1,
        "kind": "error",
        "title": "ZeroDivisionError: division by zero",
        "culprit": IsStr(regex=r"tests/project/views\.py:\d+ in error"),
        "hint": IsStr | None,
        "occurrences": 1,
        "first_seen": IsPositiveInt,
        "last_seen": IsPositiveInt,
        "last_trace_id": IsStr,
        "endpoints": ["GET /error/"],
    }

    assert [(issue["kind"], issue["endpoints"]) for issue in recorded.list_issues()] == IsList(
        ("error", ["GET /error/"]),
        ("duplicates", ["GET /duplicates/"]),
        ("n+1", ["GET /n-plus-one/"]),
        ("n+1", ["GET /n-plus-one/"]),
        check_order=False,
    )
    assert recorded.list_issues("kind:error") == [error]
    assert recorded.list_issues("kind:error route:/err*") == [error]
    assert recorded.list_issues("Division since:1h") == [error]
    assert recorded.list_issues("kind:error route:/fixed/") == []
    assert client.get("/__tracepanel__/api/issues", {"q": "kind:error"}).json() == {"issues": [error]}


@pytest.mark.django_db
def test_counts(client: Client, recorded: Store) -> None:
    # Two N+1 patterns on /n-plus-one/, one duplicate group, one exception.
    counts = {
        "issues": 4,
        "errors": 1,
        "sections": {"http": 6, "graphql": 0, "tasks": 0, "websockets": 0, "jobs": 0},
    }
    assert recorded.counts() == counts
    assert client.get("/__tracepanel__/api/counts").json() == counts


@pytest.mark.django_db
def test_issue_detail(client: Client, recorded: Store) -> None:
    (error,) = recorded.list_issues("kind:error")

    assert recorded.issue_detail(str(error["fingerprint"]), buckets=4) == {
        **error,
        # From the first event up to now: the only event opens the series.
        "series": [IsPartialDict(calls=1, errors=1), *[IsPartialDict(calls=0)] * 3],
        "traces": [IsPartialDict(id=error["last_trace_id"], path="/error/", http_status=500)],
    }
    assert recorded.issue_detail("nope") is None
    assert client.get(f"/__tracepanel__/api/issues/detail?fingerprint={error['fingerprint']}").json() == IsPartialDict(
        title="ZeroDivisionError: division by zero", endpoints=["GET /error/"]
    )


@pytest.mark.django_db
def test_log_attrs(recorded: Store) -> None:
    (log,) = recorded.search_logs("careful")[0]

    assert log == IsPartialDict(
        message="careful with fire",
        attrs={
            "code.function": "logs",
            "code.filepath": "tests/project/views.py",
            "code.lineno": IsPositiveInt,
            "thread.name": IsStr,
            "process.pid": IsPositiveInt,
            "log.template": "careful with %s",
            "extra.order_id": 42,
            "extra.payment_token": "********",
            "extra.tags": "['a']",
        },
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("query", "messages"),
    [
        ("extra.order_id:42", ["careful with fire"]),
        ("extra.order_id:4*", ["careful with fire"]),
        ("extra.order_id:7", []),
        ("extra.missing:1", []),
        ("user:nobody", []),
    ],
)
def test_log_extra_and_user_filters(recorded: Store, query: str, messages: list[str]) -> None:
    assert [log["message"] for log in recorded.search_logs(query)[0]] == messages


@pytest.mark.django_db
def test_endpoints_and_queries_in_a_time_window(recorded: Store) -> None:
    assert [len(recorded.list_endpoints(query)) for query in ("since:1h", "until:1h", "since:1h route:/error/")] == [
        6,
        0,
        1,
    ]
    assert len(recorded.list_queries("since:1h")) == len(recorded.list_queries())
    assert recorded.list_queries("until:1h") == []
