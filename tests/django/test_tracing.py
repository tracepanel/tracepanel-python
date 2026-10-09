import os
import subprocess
import sys

import pytest
from dirty_equals import IsPartialDict
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore, issues
from tracepanel.core import conf
from tracepanel.core.conf import ConfigError
from tracepanel.core.model import Trace
from tracepanel.django.middleware import route_template

pytestmark = pytest.mark.django_db


def only(traces: list[Trace]) -> Trace:
    assert len(traces) == 1
    return traces[0]


def kinds(trace: Trace) -> set[str]:
    return {issue.kind for issue in issues(trace)}


def test_request_trace_matches_schema(
    client: Client, traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    client.get("/n-plus-one/")
    trace = only(traces)
    validator.validate(trace.to_dict())
    assert trace.kind == "http"
    assert trace.name == "GET /n-plus-one/"
    assert trace.attrs["http.route"] == "/n-plus-one/"
    assert trace.attrs["code.view"] == "tests.project.views.n_plus_one"
    assert trace.attrs["http.status_code"] == 200
    root = trace.spans[0]
    assert root.parent_id is None
    assert root.kind == "http"
    (view,) = [span for span in trace.spans if span.kind == "view"]
    assert view.parent_id == root.id
    # Queries made by the view nest under its span.
    assert all(span.parent_id == view.id for span in trace.spans if span.kind == "sql")


def test_n_plus_one_with_hints(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/n-plus-one/")
    found = {i.title.split(" · ")[1]: i for i in issues(only(traces)) if i.kind == "n+1"}
    content_type = found["SELECT django_content_type"]
    assert content_type.count == 12
    assert content_type.hint is not None
    assert "select_related" in content_type.hint
    assert content_type.culprit is not None
    assert content_type.culprit.startswith("tests/project/views.py:")
    groups_hint = found["SELECT auth_group"].hint
    assert groups_hint is not None
    assert "prefetch_related" in groups_hint


def test_fixed_queries_have_no_issues(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/fixed/")
    trace = only(traces)
    assert issues(trace) == []
    # permissions + content types in one JOIN, users, groups prefetch
    assert sum(1 for s in trace.spans if s.kind == "sql") == 3


def test_duplicates(client: Client, traces: list[Trace], db: None) -> None:
    client.get("/duplicates/")
    assert kinds(only(traces)) == {"duplicates"}


def test_slow_sql(client: Client, traces: list[Trace], db: None, settings: Settings) -> None:
    settings.TRACEPANEL = {"slow_sql_ms": 0}
    client.get("/duplicates/")
    assert "slow-sql" in kinds(only(traces))


def test_unknown_setting_is_rejected(client: Client, traces: list[Trace], db: None, settings: Settings) -> None:
    settings.TRACEPANEL = {"slow_sql": 0}
    with pytest.raises(ValueError, match="slow_sql"):
        client.get("/duplicates/")


def test_sql_span_attributes(client: Client, traces: list[Trace], db: None) -> None:
    client.get("/duplicates/")
    span = next(s for s in only(traces).spans if s.kind == "sql")
    assert span.name == "SELECT COUNT auth_user"
    assert span.attrs["db.vendor"] == connection.vendor
    assert str(span.attrs["db.statement"]).startswith("SELECT COUNT(*)")
    assert str(span.attrs["code.origin"]).startswith("tests/project/views.py:")
    assert "params" not in span.attrs


def test_cache_spans(client: Client, traces: list[Trace], db: None) -> None:
    client.get("/cache/")
    spans = [s for s in only(traces).spans if s.kind == "cache"]
    assert [(s.name, s.attrs.get("cache.hit")) for s in spans] == [
        ("cache.set", None),
        ("cache.get", True),
        ("cache.get", False),
    ]
    assert spans[0].attrs["cache.key"] == "answer"


def test_logs(client: Client, traces: list[Trace], db: None) -> None:
    client.get("/logs/")
    trace = only(traces)
    assert [(log.level, log.message) for log in trace.logs] == [
        ("info", "hello world"),
        ("warning", "careful with fire"),
        ("error", "parse failed"),
    ]
    assert trace.logs[2].exception is not None
    assert "ValueError" in trace.logs[2].exception
    view = next(span for span in trace.spans if span.kind == "view")
    assert trace.logs[0].span_id == view.id


def test_error(client: Client, traces: list[Trace], db: None, validator: Draft202012Validator) -> None:
    client.raise_request_exception = False
    response = client.get("/error/")
    trace = only(traces)
    validator.validate(trace.to_dict())
    assert response.status_code == 500
    assert trace.status == "error"
    error = trace.errors[0]
    assert error.type == "builtins.ZeroDivisionError"
    assert error.frames[-1].in_app
    assert error.frames[-1].function == "error"
    issue = issues(trace)[0]
    assert issue.kind == "error"
    assert issue.culprit is not None
    assert issue.culprit.startswith("tests/project/views.py:")


def test_authenticated_user(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.force_login(users[0])
    client.get("/me/")
    attrs = only(traces).attrs

    assert {key: value for key, value in attrs.items() if key.startswith("user.")} == {
        "user.id": str(users[0].pk),
        "user.username": "user0",
        "user.is_staff": False,
        "user.is_superuser": False,
    }


def test_untouched_user_costs_no_queries(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.force_login(users[0])
    client.get("/untouched-user/")
    trace = only(traces)
    assert "user.id" not in trace.attrs
    assert not [s for s in trace.spans if s.kind == "sql"]


def test_tracepanel_urls_are_not_traced(client: Client, traces: list[Trace], store: AgentStore) -> None:
    client.get("/__tracepanel__/api/traces")
    assert traces == []


def test_setting_with_wrong_type_is_rejected(client: Client, db: None, settings: Settings) -> None:
    settings.TRACEPANEL = {"slow_sql_ms": "fast"}
    with pytest.raises(ConfigError, match="framework settings: slow_sql_ms must be a whole number, got 'fast'"):
        client.get("/duplicates/")


def test_environment_overrides_settings(
    client: Client, traces: list[Trace], db: None, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.TRACEPANEL = {"slow_sql_ms": 10_000}
    monkeypatch.setenv("TRACEPANEL_SLOW_SQL_MS", "0")
    conf.reset()  # variables are read when the configuration loads
    client.get("/duplicates/")
    assert "slow-sql" in kinds(only(traces))


def test_error_traceback_has_source_locals_and_chain(
    client: Client, traces: list[Trace], db: None, validator: Draft202012Validator
) -> None:
    client.raise_request_exception = False
    client.get("/chained-error/")
    trace = only(traces)
    validator.validate(trace.to_dict())
    error = trace.errors[0]
    assert error.type == "tests.project.views.PaymentError"
    frame = error.frames[-1]
    assert frame.in_app
    assert frame.context_start is not None
    assert any("raise PaymentError(msg) from exc" in line for line in frame.context)
    assert frame.locals is not None
    assert frame.locals["amount"] == "42"
    assert frame.locals["api_token"] == "********"
    (cause,) = error.chain
    assert cause.relation == "cause"
    assert cause.type == "builtins.KeyError"
    assert cause.frames[-1].function == "chained_error"


def test_locals_can_be_disabled(client: Client, traces: list[Trace], db: None, settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_locals": False}
    client.raise_request_exception = False
    client.get("/chained-error/")
    assert all(frame.locals is None for frame in only(traces).errors[0].frames)


def test_view_and_template_spans_nest_sql(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/users-page/")
    trace = only(traces)
    by_id = {span.id: span for span in trace.spans}
    (view,) = [s for s in trace.spans if s.kind == "view"]
    assert view.name == "tests.project.views.users_page"
    assert view.parent_id == trace.spans[0].id
    templates = [s for s in trace.spans if s.kind == "template"]
    page = next(s for s in templates if s.name == "tests/users.html")
    assert page.parent_id == view.id
    assert sum(1 for s in templates if s.name == "tests/badge.html") == len(users)
    group_queries = [s for s in trace.spans if s.kind == "sql" and s.name == "SELECT auth_group"]
    assert all(by_id[str(s.parent_id)].id == page.id for s in group_queries)
    issue = next(i for i in issues(trace) if i.kind == "n+1")
    assert issue.culprit is not None
    assert issue.culprit.startswith("tests/users.html (rendered from tests/project/views.py:")


@pytest.mark.django_db
def test_site_root_is_an_endpoint(client: Client, traces: list[Trace]) -> None:
    client.get("/")
    (trace,) = traces

    assert (trace.name, trace.attrs["http.route"]) == ("GET /", "/")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("setting", "path", "count"),
    [
        ({}, "/health/", 0),
        ({}, "/fixed/", 1),
        ({"ignore_paths": ["/fixed/*"]}, "/fixed/", 0),
        ({"ignore_paths": []}, "/health/", 1),
    ],
)
def test_ignored_paths(  # noqa: PLR0913, PLR0917 (fixtures plus the parameters)
    client: Client, traces: list[Trace], settings: Settings, setting: dict[str, object], path: str, count: int
) -> None:
    settings.TRACEPANEL = setting

    client.get(path)

    assert len(traces) == count


@pytest.mark.django_db
def test_ignored_loggers(client: Client, traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"ignore_loggers": ["tests"]}

    client.get("/logs/")

    assert only(traces).logs == []


@pytest.mark.django_db
def test_a_500_behind_a_logged_exception_is_its_error(client: Client, traces: list[Trace]) -> None:
    client.get("/caught-and-logged/")
    client.get("/caught-and-logged/?divisor=1")
    failed, fine = traces

    assert (failed.status, [(e.type, e.handled) for e in failed.errors]) == (
        "error",
        [("builtins.ZeroDivisionError", False)],
    )
    assert [issue.kind for issue in issues(failed)] == ["error"]
    assert (fine.status, fine.errors) == ("ok", [])


@pytest.mark.django_db
def test_logout_keeps_the_user_who_left(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.force_login(users[0])

    client.get("/sign-out/")
    (trace,) = traces

    assert trace.attrs == IsPartialDict(
        {"user.id": str(users[0].pk), "user.username": "user0", "user.logged_out": True}
    )


@pytest.mark.parametrize(
    ("route", "template"),
    [
        ("users/<int:pk>/", "users/<int:pk>/"),
        ("drf/users/$", "drf/users/"),
        ("^drf/users/(?P<pk>[^/.]+)/$", "drf/users/<pk>/"),
        ("users\\.(?P<format>[a-z0-9]+)/?$", "users.<format>/"),
        ("^archive/(?P<year>(19|20)\\d{2})/$", "archive/<year>/"),
    ],
)
def test_regex_routes_read_like_paths(route: str, template: str) -> None:
    assert route_template(route) == template


COUNT_TRACES = """
import django

django.setup()
from django.test import Client
from tracepanel.storage import transport

traces = []


class Writer:
    def submit(self, trace):
        traces.append(trace)

    def flush(self, timeout=5.0):
        pass


transport.set_writer(Writer())
Client().get("/logs/")
print(len(traces))
"""


@pytest.mark.parametrize(("enabled", "traced"), [(None, "0"), ("1", "1")])
def test_without_debug_only_when_turned_on(enabled: str | None, traced: str) -> None:
    # Installing happens once per process, at startup: a fresh interpreter with DEBUG off.
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("TRACEPANEL_")},
        "DJANGO_SETTINGS_MODULE": "tests.project.settings_no_debug",
        **({"TRACEPANEL_ENABLED": enabled} if enabled else {}),
    }
    result = subprocess.run(  # noqa: S603 (our own interpreter)
        [sys.executable, "-c", COUNT_TRACES], capture_output=True, text=True, env=env, check=False
    )

    assert (result.returncode, result.stdout.strip().splitlines()[-1:]) == (0, [traced]), result.stderr[-2000:]
