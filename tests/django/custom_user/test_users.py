"""User switching with a custom user model; run by tests/django/test_users.py under settings_custom_user."""

import pytest
from django.conf import settings

if settings.AUTH_USER_MODEL != "members.Member":
    pytest.skip("needs tests.project.settings_custom_user", allow_module_level=True)

from dirty_equals import IsUUID
from django.test import Client

from tests.project.members.models import Member
from tracepanel.core.model import Trace

API = "/__tracepanel__/api/users"
WIDGET = {"X-Tracepanel": "1"}


@pytest.fixture
def members(db: None) -> list[Member]:
    return [Member.objects.create_user(f"m{i}@example.com", nickname=f"member {i}") for i in range(3)]


def switch(client: Client, user_id: object) -> tuple[int, object]:
    response = client.post(f"{API}/switch", {"user_id": user_id}, content_type="application/json", headers=WIDGET)
    return response.status_code, response.json()


def test_search_by_the_login_field(client: Client, members: list[Member]) -> None:
    response = client.get(API, {"q": "m1@"})

    assert response.json() == {
        "users": [
            {
                "id": str(members[1].pk),
                "username": "m1@example.com",
                "name": None,
                "email": "m1@example.com",
                "is_staff": False,
                "is_superuser": False,
            }
        ]
    }


def test_switch_in_and_out(client: Client, members: list[Member], traces: list[Trace]) -> None:
    target = members[2]

    assert switch(client, str(target.pk)) == (200, {"user": {"id": str(target.pk), **_described(target)}})
    assert client.get(f"{API}/me").json() == {"user": {"id": str(target.pk), **_described(target)}}

    client.get("/me/")

    assert traces[-1].attrs == {
        **traces[-1].attrs,
        "user.id": IsUUID,
        "user.username": "m2@example.com",
        "user.is_staff": False,
        "user.is_superuser": False,
    }

    assert switch(client, None) == (200, {"user": None})
    assert client.get(f"{API}/me").json() == {"user": None}


def test_unknown_or_malformed_id(client: Client, members: list[Member]) -> None:
    del members

    assert switch(client, "00000000-0000-0000-0000-000000000000") == (404, {"error": "no such user"})
    assert switch(client, "not-a-uuid") == (404, {"error": "no such user"})


def _described(member: Member) -> dict[str, object]:
    return {
        "username": member.email,
        "name": None,
        "email": member.email,
        "is_staff": False,
        "is_superuser": False,
    }
