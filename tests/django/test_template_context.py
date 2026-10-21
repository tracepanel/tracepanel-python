"""A template span keeps the context the template got: names, masked values, never a query to show them."""

from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest
from dirty_equals import IsStr
from django.contrib.auth.models import Group, User
from django.template import Context, Template
from django.test import Client
from pytest_django.fixtures import Settings

from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace

pytestmark = pytest.mark.django_db

AssertQueries = Callable[[int], AbstractContextManager[object]]


def templates(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "template"]


@pytest.fixture
def processors(settings: Settings) -> None:
    """The context processors a project has: the request, the user and permissions (Django adds the CSRF token's)."""
    settings.TEMPLATES = [
        {
            **settings.TEMPLATES[0],
            "OPTIONS": {
                "context_processors": [
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                ]
            },
        }
    ]


@pytest.fixture
def people(db: None) -> None:
    User.objects.create_user("ann", password="x")
    Group.objects.create(name="staff")


def test_a_template_keeps_its_context_without_a_query_more(
    client: Client,
    traces: list[Trace],
    processors: None,
    people: None,
    django_assert_num_queries: AssertQueries,
) -> None:
    # The view's own two queries: the groups, the first user. Not the lazy users, nor the count Counted would run.
    with django_assert_num_queries(2):
        response = client.get("/context-page/")

    assert response.status_code == 200

    (trace,) = traces
    page, badge = templates(trace)

    assert (page.name, badge.name) == ("tests/context.html", "tests/badge.html")

    assert page.attrs == {
        "template.name": "tests/context.html",
        "template.context.names": [
            "csrf_token",
            "request",
            "user",
            "perms",
            "title",
            "users",
            "groups",
            "first",
            "counted",
            "api_key",
        ],
        "template.context.values": [
            "********",
            "<WSGIRequest: GET '/context-page/'>",
            IsStr(regex=r"<SimpleLazyObject: .*>"),
            IsStr(regex=r"PermWrapper\(<SimpleLazyObject: .*>\)"),
            "'Users'",
            "<QuerySet of User, not evaluated>",
            "<QuerySet of Group: 1 row>",
            "<User: ann>",
            "<Counted: not shown, it would query the database>",
            "********",
        ],
    }

    # An include gets its own span and context; the template it extends renders in its span, not one of its own.
    assert badge.attrs == {
        "template.name": "tests/badge.html",
        "template.context.names": [
            "csrf_token",
            "request",
            "user",
            "perms",
            "title",
            "users",
            "groups",
            "first",
            "counted",
            "api_key",
        ],
        "template.context.values": [
            "********",
            "<WSGIRequest: GET '/context-page/'>",
            # {% include ... with user=first %}
            "<User: ann>",
            IsStr(regex=r"PermWrapper\(<SimpleLazyObject: .*>\)"),
            "'Users'",
            "<QuerySet of User, not evaluated>",
            "<QuerySet of Group: 1 row>",
            "<User: ann>",
            "<Counted: not shown, it would query the database>",
            "********",
        ],
    }
    assert badge.parent_id == page.id


def test_the_context_can_be_turned_off(client: Client, traces: list[Trace], people: None, settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_template_context": False}

    client.get("/context-page/")

    assert [span.attrs for span in templates(traces[0])] == [
        {"template.name": "tests/context.html"},
        {"template.name": "tests/badge.html"},
    ]


def test_a_template_rendered_by_hand_keeps_its_context() -> None:
    with start_trace("http", "GET /") as trace:
        Template("{{ a }}").render(Context({"a": 1, "password": "pw", **{f"n{i}": i for i in range(45)}}))

    (span,) = templates(trace)

    assert span.attrs == {
        "template.name": "<string template>",
        "template.context.names": ["a", "password", *[f"n{i}" for i in range(38)]],
        "template.context.values": ["1", "********", *[str(i) for i in range(38)]],
        "template.context.omitted": 7,
    }
