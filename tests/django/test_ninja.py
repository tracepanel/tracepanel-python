import pytest

pytest.importorskip("ninja")

from asgiref.sync import async_to_sync
from dirty_equals import IsPartialDict, IsStr
from django.contrib.auth.models import User
from django.test import AsyncClient, Client
from jsonschema import Draft202012Validator

from tracepanel.core.model import Trace


def steps(trace: Trace) -> list[tuple[str, str]]:
    return [(span.kind, span.name) for span in trace.spans if span.kind in {"view", "framework"}]


@pytest.mark.django_db
def test_operation_names_the_view(
    client: Client, traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    response = client.get("/ninja/users?limit=2")
    (trace,) = traces

    assert response.json() == [{"username": "user0", "groups": ["staff"]}, {"username": "user1", "groups": ["staff"]}]
    assert trace.name == "GET /ninja/users"
    assert trace.attrs == IsPartialDict(
        {
            "http.route": "/ninja/users",
            "code.view": "tests.project.ninja.list_users",
            "ninja.operation_id": IsStr(regex=r".*list_users"),
        }
    )

    assert steps(trace) == [
        ("view", "tests.project.ninja.list_users"),
        ("framework", "ninja validate"),
        ("framework", "ninja response"),
    ]

    validator.validate(trace.to_dict())


@pytest.mark.django_db
def test_queries_while_serializing_belong_to_the_response(
    client: Client, traces: list[Trace], users: list[User]
) -> None:
    client.get("/ninja/users?limit=3")
    (trace,) = traces
    response = next(span for span in trace.spans if span.name == "ninja response")
    queries = [span for span in trace.spans if span.kind == "sql" and span.parent_id == response.id]

    assert len(queries) == 3


@pytest.mark.django_db
def test_another_method_on_the_same_path_is_its_own_operation(client: Client, traces: list[Trace]) -> None:
    client.post("/ninja/users", {"name": "staff"}, content_type="application/json")
    (trace,) = traces

    assert (trace.name, trace.attrs["code.view"], trace.attrs["ninja.operation_id"]) == (
        "POST /ninja/users",
        "tests.project.ninja.create_group",
        "create_group",
    )


@pytest.mark.django_db
def test_invalid_input_lists_what_was_wrong(client: Client, traces: list[Trace]) -> None:
    response = client.get("/ninja/users?limit=abc")
    (trace,) = traces
    (validate,) = [span for span in trace.spans if span.name == "ninja validate"]

    assert (response.status_code, trace.status, trace.errors) == (422, "ok", [])
    assert (validate.status, validate.attrs) == (
        "error",
        {
            "framework.step": "validate",
            "validation.errors": ["query.limit: Input should be a valid integer, unable to parse string as an integer"],
        },
    )


@pytest.mark.django_db
def test_auth_refusal_names_the_callbacks(client: Client, traces: list[Trace]) -> None:
    response = client.get("/ninja/me")
    (trace,) = traces
    (auth,) = [span for span in trace.spans if span.name == "ninja auth"]

    assert response.status_code == 401
    assert auth.attrs == {
        "framework.step": "auth",
        "auth.callbacks": ["ninja.security.session.SessionAuth"],
        "auth.result": "refused",
    }


@pytest.mark.django_db
def test_auth_passes(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.force_login(users[0])
    client.get("/ninja/me")
    trace = traces[-1]
    (auth,) = [span for span in trace.spans if span.name == "ninja auth"]

    assert (trace.attrs["http.status_code"], auth.attrs["auth.result"]) == (200, "ok")


@pytest.mark.django_db(transaction=True)
def test_async_operation(traces: list[Trace]) -> None:
    response = async_to_sync(AsyncClient().get)("/ninja/count")
    (trace,) = traces

    assert response.json() == {"users": 0}
    assert steps(trace) == [
        ("view", "tests.project.ninja.count_users"),
        ("framework", "ninja validate"),
        ("framework", "ninja response"),
    ]
