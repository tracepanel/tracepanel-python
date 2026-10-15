import re
from datetime import UTC, datetime

import pytest
from dirty_equals import IsInt, IsList, IsPartialDict, IsPositiveFloat, IsPositiveInt, IsStr
from django.contrib.auth.models import User
from django.test import Client

from tests.agentstore import AgentStore, SearchError, newer
from tracepanel.core import conf
from tracepanel.core.context import record_span, start_trace
from tracepanel.core.model import Log
from tracepanel.storage.transport import set_writer


def test_unknown_filter_and_bad_values_are_explained(store: AgentStore) -> None:
    with pytest.raises(SearchError, match="Unknown filter 'colour'"):
        store.search_traces("colour:red")
    with pytest.raises(SearchError, match="status expects a number"):
        store.search_traces("status>=lots")
    with pytest.raises(SearchError, match="has expects"):
        store.search_traces("has:bugs")
    with pytest.raises(SearchError, match="duration expects a duration"):
        store.search_traces("duration>fast")


@pytest.fixture
def recorded(client: Client, users: list[User], store: AgentStore, monkeypatch: pytest.MonkeyPatch) -> AgentStore:
    """A few real requests written to the store."""
    # A slow runner's first queries on a new database can pass the default threshold: these lists expect no slow SQL.
    monkeypatch.setenv("TRACEPANEL_SLOW_SQL_MS", "60000")
    conf.reset()
    store.max_traces = 100
    writer = store.writer()
    set_writer(writer)
    client.raise_request_exception = False
    try:
        for path in ("/n-plus-one/", "/fixed/", "/duplicates/", "/logs/", "/error/", "/cache/"):
            client.get(path)
        writer.flush()
    finally:
        set_writer(None)
    return store


def names(store: AgentStore, query: str) -> list[str]:
    items, _ = store.search_traces(query)
    return sorted(str(item["path"]) for item in items)


@pytest.mark.django_db
def test_trace_filters(recorded: AgentStore) -> None:
    assert names(recorded, "status>=500") == ["/error/"]
    assert names(recorded, "has:n+1") == ["/n-plus-one/"]
    assert names(recorded, "has:duplicates") == ["/duplicates/"]
    assert names(recorded, "has:logs route:/logs/") == ["/logs/"]
    assert names(recorded, "path:/n-* ") == ["/n-plus-one/"]
    assert names(recorded, "method:get queries>5 has:issues") == ["/n-plus-one/"]
    assert "/fixed/" in names(recorded, "duration<10s status:200")
    assert names(recorded, "plus") == ["/n-plus-one/"]


@pytest.mark.django_db
def test_trace_pagination(recorded: AgentStore) -> None:
    first, cursor = recorded.search_traces("", limit=4)
    assert len(first) == 4
    assert cursor is not None
    rest, end = recorded.search_traces("", limit=4, before=cursor)
    assert end is None
    assert {str(item["id"]) for item in first}.isdisjoint(str(item["id"]) for item in rest)
    assert len(first) + len(rest) == 6


@pytest.mark.django_db
def test_log_search(recorded: AgentStore) -> None:
    logs, _ = recorded.search_logs("hello")
    assert [log["message"] for log in logs] == ["hello world"]
    assert logs[0]["path"] == "/logs/"
    errors, _ = recorded.search_logs("level>=error")
    assert {log["message"] for log in errors} >= {"parse failed"}
    assert recorded.search_logs("logger:tests.* careful")[0][0]["level"] == "warning"


@pytest.mark.django_db
def test_query_and_endpoint_aggregates(recorded: AgentStore) -> None:
    # The N+1 statement: the one called once per user (ties in total time make list order unreliable).
    n_plus_one = max(recorded.list_calls("sql", "auth_group"), key=lambda query: int(str(query["calls"])))
    assert n_plus_one == IsPartialDict(calls=5, most_per_trace=5, origin=IsStr(regex=r"tests/project/views\.py:.*"))
    key = str(n_plus_one["key"])
    assert names(recorded, f"call:{key}") == ["/n-plus-one/"]

    endpoints = {str(e["route"]): e for e in recorded.list_endpoints()}
    assert endpoints["/error/"]["errors"] == 1
    assert endpoints["/n-plus-one/"]["with_issues"] == 1
    assert endpoints["/n-plus-one/"]["calls"] == 1


@pytest.mark.django_db
def test_api(client: Client, recorded: AgentStore) -> None:
    body = client.get("/__tracepanel__/api/traces", {"q": "has:n+1"}).json()
    assert [t["path"] for t in body["traces"]] == ["/n-plus-one/"]
    assert body["next"] is None
    assert client.get("/__tracepanel__/api/traces", {"q": "nope:1"}).status_code == 400
    assert client.get("/__tracepanel__/api/logs", {"q": "careful"}).json()["logs"][0]["message"] == "careful with fire"
    assert client.get("/__tracepanel__/api/calls", {"system": "sql"}).json()["calls"]
    assert client.get("/__tracepanel__/api/endpoints").json()["endpoints"]
    assert client.get("/__tracepanel__/api/issues", {"q": "kind:error"}).json()["issues"][0]["kind"] == "error"
    assert client.get("/__tracepanel__/api/search").json()["traces"]["status"]["type"] == "number"


@pytest.mark.django_db
def test_endpoint_detail(client: Client, recorded: AgentStore) -> None:
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


def test_traces_overview(client: Client, recorded: AgentStore) -> None:
    operation = {
        "kind": "http",
        "name": IsStr(regex=r"GET /.*"),
        "method": "GET",
        "route": IsStr,
        "view": None,
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
            # Findings only: the failing request counts in `errors`.
            "with_issues": 2,
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


def test_time_filters(recorded: AgentStore) -> None:
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
def test_time_filter_errors(recorded: AgentStore, query: str, message: str) -> None:
    with pytest.raises(SearchError, match=re.escape(message)):
        recorded.search_traces(query)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("path:/fixed/,/logs/", ["/fixed/", "/logs/"]),
        ("path:/n-*,/dup*", ["/duplicates/", "/n-plus-one/"]),
        ("path!=/fixed/,/logs/ status<500", ["/cache/", "/duplicates/", "/n-plus-one/"]),
        # has:issues is findings; the failing request is has:error.
        ("status:200,500 has:issues", ["/duplicates/", "/n-plus-one/"]),
        ("method:get,post route:/error/", ["/error/"]),
    ],
)
def test_any_of_values(recorded: AgentStore, query: str, expected: list[str]) -> None:
    assert names(recorded, query) == expected


@pytest.mark.django_db
def test_any_of_levels(recorded: AgentStore) -> None:
    logs, _ = recorded.search_logs("level:warning,error")

    assert sorted((log["level"], log["message"]) for log in logs) == [
        ("error", "Internal Server Error: /error/"),
        ("error", "parse failed"),
        ("warning", "careful with fire"),
    ]

    with pytest.raises(SearchError, match=re.escape("level expects a level")):
        recorded.search_logs("level:warning,loud")


# The test project's views query the database and cache through Django's cache (locmem); no Redis or HTTP service.
DATABASE = {"system": "sql", "family": "sql", "label": "Database"}
SYSTEMS = {"systems": [DATABASE, {"system": "cache", "family": "cache", "label": "Cache"}]}


@pytest.mark.django_db
def test_known_values(client: Client, recorded: AgentStore) -> None:
    no_tasks: list[str] = []
    expected = {
        "route": ["/cache/", "/duplicates/", "/error/", "/fixed/", "/logs/", "/n-plus-one/"],
        "logger": IsList("django.request", "tests.project.views", check_order=False),
        "db": ["default"],
        "targets": {"cache": ["default"], "sql": ["default"]},
        "ops": {"cache": ["get", "set"], "sql": ["SELECT"]},
        "task": no_tasks,
        "queue": no_tasks,
        "job": no_tasks,
        "rpc": no_tasks,
        "page": no_tasks,
        # django.request adds request and status_code to its own records.
        "extra": ["extra.order_id", "extra.payment_token", "extra.request", "extra.status_code", "extra.tags"],
    }

    known = recorded.known_values()
    expected = newer(
        expected,
        known,
        {
            # What ran the tasks: none in the test project.
            "task_system": no_tasks,
            # The routes whose requests logged anything: the logs' Route menu.
            "log_route": ["/error/", "/logs/"],
            # What the records were logged inside: the view that wrote them.
            "in": ["view"],
            "span": ["tests.project.views.logs"],
        },
    )

    assert known == expected
    assert client.get("/__tracepanel__/api/values").json() == expected


@pytest.mark.django_db
def test_overview_spans_the_searched_window(recorded: AgentStore) -> None:
    # The overview's 60 buckets span the window searched, here an empty one.
    assert recorded.traces_overview("since:2h until:1h") == {
        "summary": None,
        "series": [IsPartialDict(calls=0, errors=0, p50=None, p95=None)] * 60,
        "histogram": [],
        "slow_threshold": 500_000,
        "kinds": [],
        "slowest": [],
        "failing": [],
    }


@pytest.mark.django_db
def test_issues(client: Client, recorded: AgentStore) -> None:
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
def test_counts(client: Client, recorded: AgentStore) -> None:
    # Two N+1 patterns on /n-plus-one/ and one duplicate group; the exception is an error, not an issue.
    counts = {
        "issues": 3,
        "errors": 1,
        "sections": {
            "http": 6,
            "graphql": 0,
            "tasks": 0,
            "websockets": 0,
            "jobs": 0,
            "rpc": 0,
            "pages": 0,
            **SYSTEMS,
        },
    }
    answer = recorded.counts()
    counts["sections"] = newer(counts["sections"], answer["sections"], {"mails": 0})

    assert answer == counts
    assert client.get("/__tracepanel__/api/counts").json() == counts


@pytest.mark.django_db
def test_issue_detail(client: Client, recorded: AgentStore) -> None:
    (error,) = recorded.list_issues("kind:error")

    detail = recorded.issue_detail(str(error["fingerprint"]))
    assert detail is not None
    assert detail == {
        **error,
        # 40 buckets up to now, from the first event or, an agent after 0.2.0, at least 10 minutes back.
        "series": IsList(*[IsPartialDict(calls=IsInt(ge=0))] * 40),
        "traces": [IsPartialDict(id=error["last_trace_id"], path="/error/", http_status=500)],
    }
    assert [bucket for bucket in detail["series"] if bucket["calls"]] == [IsPartialDict(calls=1, errors=1)]
    assert recorded.issue_detail("nope") is None
    assert client.get(f"/__tracepanel__/api/issues/detail?fingerprint={error['fingerprint']}").json() == IsPartialDict(
        title="ZeroDivisionError: division by zero", endpoints=["GET /error/"]
    )


@pytest.mark.django_db
def test_log_attrs(recorded: AgentStore) -> None:
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
def test_log_extra_and_user_filters(recorded: AgentStore, query: str, messages: list[str]) -> None:
    assert [log["message"] for log in recorded.search_logs(query)[0]] == messages


@pytest.mark.django_db
def test_endpoints_and_queries_in_a_time_window(recorded: AgentStore) -> None:
    assert [len(recorded.list_endpoints(query)) for query in ("since:1h", "until:1h", "since:1h route:/error/")] == [
        6,
        0,
        1,
    ]
    assert len(recorded.list_calls("sql", "since:1h")) == len(recorded.list_calls("sql"))
    assert recorded.list_calls("sql", "until:1h") == []


@pytest.fixture
def two_apps(recorded: AgentStore) -> AgentStore:
    """The Django requests, and one request of another app (service.name "shop") sending to the same agent."""
    with start_trace("http", "GET /cart/") as trace:
        trace.attrs.update(
            {
                "service.name": "shop",
                "service.role": "web",
                "http.method": "GET",
                "http.route": "/cart/",
                "http.path": "/cart/",
                "http.status_code": 200,
            }
        )
        record_span("sql", "SELECT cart", start=trace.start, duration=10, attrs={"db.statement": "SELECT 1 FROM cart"})
        trace.logs.append(Log(span_id=None, time=trace.start, level="INFO", logger="shop", message="cart shown"))
    recorded.write([trace])
    return recorded


@pytest.mark.django_db
def test_one_app_of_several(client: Client, two_apps: AgentStore) -> None:
    django = conf.get_config().service
    every = {
        "issues": 3,
        "errors": 1,
        "sections": {
            "http": 7,
            "graphql": 0,
            "tasks": 0,
            "websockets": 0,
            "jobs": 0,
            "rpc": 0,
            "pages": 0,
            **SYSTEMS,
        },
    }

    assert names(two_apps, "service:shop") == ["/cart/"]
    assert names(two_apps, f"service:{django}") == [
        "/cache/",
        "/duplicates/",
        "/error/",
        "/fixed/",
        "/logs/",
        "/n-plus-one/",
    ]

    assert [row["route"] for row in two_apps.list_endpoints("service:shop")] == ["/cart/"]
    assert [row["name"] for row in two_apps.list_calls("sql", "service:shop")] == ["SELECT cart"]
    assert [row["message"] for row in two_apps.search_logs("service:shop")[0]] == ["cart shown"]
    assert two_apps.list_issues("service:shop") == []
    assert len(two_apps.list_issues(f"service:{django}")) == 4

    assert sorted(row["service"] for row in two_apps.list_services("kind:app")["apps"]) == sorted([django, "shop"])
    assert [row["service"] for row in two_apps.list_services("service:shop")["apps"]] == ["shop"]

    answer = two_apps.counts()
    every["sections"] = newer(every["sections"], answer["sections"], {"mails": 0})
    assert answer == every
    shop = two_apps.get("counts", service="shop")
    assert shop == {
        "issues": 0,
        "errors": 0,
        # The shop app uses no cache.
        "sections": newer(
            {
                "http": 1,
                "graphql": 0,
                "tasks": 0,
                "websockets": 0,
                "jobs": 0,
                "rpc": 0,
                "pages": 0,
                "systems": [DATABASE],
            },
            shop["sections"],
            {"mails": 0},
        ),
    }
    assert two_apps.get("counts", service=django) == every | {
        "sections": newer(
            {"http": 6, "graphql": 0, "tasks": 0, "websockets": 0, "jobs": 0, "rpc": 0, "pages": 0, **SYSTEMS},
            shop["sections"],
            {"mails": 0},
        )
    }
    assert client.get("/__tracepanel__/api/counts", {"service": "shop"}).json() == two_apps.get(
        "counts", service="shop"
    )
