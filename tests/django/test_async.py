import pytest
from asgiref.sync import async_to_sync, iscoroutinefunction
from dirty_equals import IsInstance, IsInt, IsPartialDict, IsStr
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.test import AsyncClient

from tests.agentstore import AgentStore, issues, newer
from tracepanel.core.model import Trace
from tracepanel.django.middleware import TracepanelMiddleware


def shape(trace: Trace) -> list[tuple[str, str, str | None]]:
    """(kind, name, parent name) of every span after the root."""
    by_id = {span.id: span for span in trace.spans}
    return [
        (span.kind, span.name, by_id[span.parent_id].name if span.parent_id in by_id else None)
        for span in trace.spans[1:]
    ]


def test_middleware_follows_the_handler_mode() -> None:
    async def async_view(request: HttpRequest) -> HttpResponse:
        return HttpResponse()

    def sync_view(request: HttpRequest) -> HttpResponse:
        return HttpResponse()

    assert (
        iscoroutinefunction(TracepanelMiddleware(async_view)),
        iscoroutinefunction(TracepanelMiddleware(sync_view)),
    ) == (
        True,
        False,
    )


@pytest.mark.django_db
def test_async_view_under_asgi(traces: list[Trace], users: list[User]) -> None:
    client = AsyncClient()
    async_to_sync(client.aforce_login)(users[0])

    response = async_to_sync(client.get)("/async/users/")
    (trace,) = traces

    assert response.json() == IsPartialDict({"me": "user0", "groups": 1, "staff": 0})
    assert (trace.name, trace.status, trace.attrs["http.status_code"]) == ("GET /async/users/", "ok", 200)
    assert trace.attrs == IsPartialDict({"user.id": str(users[0].pk), "user.username": "user0"})
    shown = [item for item in shape(trace) if item[0] != "sql" or item[2] != "tests.project.views.async_users"]
    assert shown[:2] == [
        ("view", "tests.project.views.async_users", "GET /async/users/"),
        ("signal", "tests.project.views.order_placed", "tests.project.views.async_users"),
    ]
    # Django 6.1 runs a signal's receivers concurrently under asend(): their order is not fixed.
    assert sorted(shown[2:]) == [
        ("function", "tests.project.views.count_orders", "tests.project.views.order_placed"),
        ("function", "tests.project.views.notify_shop", "tests.project.views.order_placed"),
        ("sql", "SELECT COUNT auth_user", "tests.project.views.count_orders"),
        ("sql", "SELECT COUNT auth_user", "tests.project.views.notify_shop"),
    ]
    assert sorted(name for kind, name, parent in shape(trace) if kind == "sql" and parent == trace.spans[1].name) == [
        "SELECT COUNT auth_group",
        "SELECT COUNT auth_user",
        "SELECT auth_user",
        "SELECT auth_user",
        "SELECT django_session",
    ]


@pytest.mark.django_db
def test_async_view_error(traces: list[Trace]) -> None:
    response = async_to_sync(AsyncClient(raise_request_exception=False).get)("/async/error/")
    (trace,) = traces

    assert (response.status_code, trace.status) == (500, "error")
    assert [(error.type, error.message) for error in trace.errors] == [
        ("builtins.ValueError", "failed after counting 0 users")
    ]
    assert [issue.kind for issue in issues(trace)] == ["error"]


@pytest.mark.django_db
def test_console_api_under_asgi(traces: list[Trace], store: AgentStore) -> None:
    async_to_sync(AsyncClient().get)("/")
    response = async_to_sync(AsyncClient().get)("/__tracepanel__/api/counts")

    answer = response.json()

    assert response.status_code == 200
    # Whether an app says it sends task events (an agent after 0.3.7 tells): the answer's shape is what counts here.
    assert answer == newer(
        {"issues": IsInt, "errors": IsInt, "sections": IsPartialDict(http=IsInt)},
        answer,
        {"live_tasks": IsInstance(bool)},
    )
    assert [trace.name for trace in traces] == ["GET /"]
    assert traces[0].id == IsStr


@pytest.mark.django_db
def test_async_orm_keeps_the_place_in_project_code(traces: list[Trace]) -> None:
    async_to_sync(AsyncClient().get)("/async/n-plus-one/")
    (trace,) = traces
    origins = [span.attrs["code.origin"] for span in trace.spans if span.kind == "sql"]

    assert origins == [IsStr(regex=r"tests/project/views\.py:\d+ in async_n_plus_one")] * 18
    assert sorted(issue.kind for issue in issues(trace)) == ["duplicates", "n+1"]
