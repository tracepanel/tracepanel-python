import pytest

pytest.importorskip("rest_framework")

import os
import subprocess
import sys
from collections.abc import Generator

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from jsonschema import Draft202012Validator

from tests.agentstore import issues
from tracepanel.core.model import Span, Trace


@pytest.fixture(autouse=True)
def fresh_throttles() -> Generator[None, None, None]:
    # DRF keeps throttle history in the default cache.
    cache.clear()
    yield
    cache.clear()


def framework(trace: Trace) -> list[tuple[str, str]]:
    return [(span.kind, span.name) for span in trace.spans if span.kind in {"view", "framework"}]


def span(trace: Trace, name: str) -> Span:
    (found,) = [span for span in trace.spans if span.name == name]
    return found


@pytest.mark.django_db
def test_viewset_action_names_the_view(
    client: Client, traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    response = client.get("/drf/users/")
    (trace,) = traces

    assert response.json()[:2] == [
        {"username": "user0", "groups": ["staff"]},
        {"username": "user1", "groups": ["staff"]},
    ]
    assert (trace.name, trace.attrs["code.view"]) == ("GET /drf/users/", "tests.project.drf.UserViewSet.list")

    assert framework(trace) == [
        ("view", "tests.project.drf.UserViewSet.list"),
        ("framework", "drf auth"),
        ("framework", "drf permissions"),
        ("framework", "drf serialize UserSerializer(many=True)"),
    ]

    assert span(trace, "drf auth").attrs == {
        "framework.step": "auth",
        "auth.classes": [
            "rest_framework.authentication.SessionAuthentication",
            "rest_framework.authentication.BasicAuthentication",
        ],
        "auth.result": "anonymous",
    }
    assert span(trace, "drf serialize UserSerializer(many=True)").attrs == {
        "framework.step": "serialize",
        "serializer.class": "tests.project.drf.UserSerializer",
        "serializer.items": 5,
    }

    validator.validate(trace.to_dict())


@pytest.mark.django_db
def test_queries_while_serializing_are_the_n_plus_one(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/drf/users/")
    (trace,) = traces
    serialize = span(trace, "drf serialize UserSerializer(many=True)")
    (issue,) = issues(trace)

    # The users themselves are read there too: the queryset is lazy until the serializer iterates it.
    assert [s.name for s in trace.spans if s.kind == "sql" and s.parent_id == serialize.id] == [
        "SELECT auth_user",
        *["SELECT auth_group"] * 5,
    ]
    assert issue.kind == "n+1"


@pytest.mark.django_db
def test_object_permission_names_the_refusing_class(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.force_login(users[1])
    response = client.get(f"/drf/users/{users[1].pk}/")
    trace = traces[-1]

    assert (response.status_code, trace.name, trace.attrs["code.view"]) == (
        403,
        "GET /drf/users/<pk>/",
        "tests.project.drf.UserViewSet.retrieve",
    )
    assert span(trace, "drf object permissions").attrs == {
        "framework.step": "permissions",
        "permission.classes": ["rest_framework.permissions.AllowAny", "tests.project.drf.IsUser0"],
        "permission.refused": "tests.project.drf.IsUser0",
        "permission.message": "Only user0 can be looked at.",
    }


@pytest.mark.django_db
def test_api_view_function_and_401(client: Client, traces: list[Trace]) -> None:
    response = client.get("/drf/whoami/")
    (trace,) = traces

    assert (response.status_code, trace.attrs["code.view"]) == (403, "tests.project.drf.whoami")
    assert span(trace, "drf permissions").attrs == {
        "framework.step": "permissions",
        "permission.classes": ["rest_framework.permissions.IsAuthenticated"],
        "permission.refused": "rest_framework.permissions.IsAuthenticated",
        "permission.message": "Authentication credentials were not provided.",
    }


@pytest.mark.django_db
def test_invalid_input_lists_what_was_wrong(client: Client, traces: list[Trace]) -> None:
    response = client.post("/drf/groups/", {}, content_type="application/json")
    (trace,) = traces
    validate = span(trace, "drf validate GroupSerializer")

    assert (response.status_code, trace.status, trace.errors) == (400, "ok", [])
    assert trace.attrs["code.view"] == "tests.project.drf.GroupCreate.post"
    assert (validate.status, validate.attrs) == (
        "error",
        {
            "framework.step": "validate",
            "serializer.class": "tests.project.drf.GroupSerializer",
            "validation.errors": ["name: This field is required."],
        },
    )


@pytest.mark.django_db
def test_valid_input_then_the_response(client: Client, traces: list[Trace]) -> None:
    response = client.post("/drf/groups/", {"name": "editors"}, content_type="application/json")
    (trace,) = traces

    assert response.status_code == 201
    assert [name for _, name in framework(trace)] == [
        "tests.project.drf.GroupCreate.post",
        "drf auth",
        "drf permissions",
        "drf validate GroupSerializer",
        "drf serialize GroupSerializer",
    ]


@pytest.mark.django_db
def test_throttled_request_says_how_long_to_wait(client: Client, traces: list[Trace]) -> None:
    client.get("/drf/throttled/")
    response = client.get("/drf/throttled/")
    throttles = span(traces[-1], "drf throttles")

    assert response.status_code == 429
    assert throttles.attrs == {
        "framework.step": "throttles",
        "throttle.classes": ["tests.project.drf.OncePerMinute"],
        "throttle.wait": 60.0,
    }


@pytest.mark.django_db
def test_views_without_throttles_have_no_throttle_span(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/drf/users/")

    assert [s for s in traces[0].spans if s.name == "drf throttles"] == []


@pytest.mark.parametrize(("enabled", "patched"), [("true", "True"), ("false", "False")])
def test_can_be_turned_off(enabled: str, patched: str) -> None:
    # Patches are applied once per process, at startup: a fresh interpreter shows what the setting does.
    code = (
        "import django; django.setup()\n"
        "from rest_framework.views import APIView\n"
        "from tracepanel.core.patching import patched\n"
        "print(patched(APIView, 'initial'))"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "tests.project.settings", "TRACEPANEL_DRF_ENABLED": enabled}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)  # noqa: S603

    assert result.stdout.strip() == patched
