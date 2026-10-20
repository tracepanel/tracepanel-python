import subprocess
import sys
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from pytest_django.fixtures import Settings

API = "/__tracepanel__/api/users"
WIDGET = {"X-Tracepanel": "1"}


def switch(
    client: Client, user_id: object, headers: dict[str, str] | None = None, remote: str = "127.0.0.1"
) -> tuple[int, object]:
    response = client.post(
        f"{API}/switch", {"user_id": user_id}, content_type="application/json", headers=headers, REMOTE_ADDR=remote
    )
    return response.status_code, response.json()


def whoami(client: Client) -> str:
    return str(client.get("/me/").json()["username"])


@pytest.mark.django_db
def test_switch_logs_in_as_another_user_and_out(client: Client, users: list[User]) -> None:
    target = users[2]

    assert client.get(f"{API}/me").json() == {"user": None}
    assert switch(client, target.pk, WIDGET) == (
        200,
        {
            "user": {
                "id": str(target.pk),
                "username": "user2",
                "name": None,
                "email": None,
                "is_staff": False,
                "is_superuser": False,
            }
        },
    )
    assert whoami(client) == "user2"
    assert client.get(f"{API}/me").json()["user"]["username"] == "user2"
    assert switch(client, None, WIDGET) == (200, {"user": None})
    assert whoami(client) == ""


@pytest.mark.django_db
def test_search(client: Client, users: list[User]) -> None:
    assert [user["username"] for user in client.get(API).json()["users"]] == [f"user{i}" for i in range(5)]
    assert [user["username"] for user in client.get(API, {"q": "R3"}).json()["users"]] == ["user3"]

    User.objects.filter(username="user2").update(first_name="Ada", email="ada@example.com")

    assert [user["username"] for user in client.get(API, {"q": "ada"}).json()["users"]] == ["user2"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("headers", "remote", "status"),
    [
        ({}, "127.0.0.1", 403),
        (WIDGET, "10.0.0.5", 403),
        ({**WIDGET, "Origin": "https://evil.example"}, "127.0.0.1", 403),
        ({**WIDGET, "Origin": "http://testserver"}, "127.0.0.1", 200),
    ],
)
def test_only_the_page_on_this_machine_may_switch(
    client: Client, users: list[User], headers: dict[str, str], remote: str, status: int
) -> None:
    assert switch(client, users[0].pk, headers, remote)[0] == status


@pytest.mark.django_db
def test_internal_ips_may_switch(client: Client, users: list[User], settings: Settings) -> None:
    settings.INTERNAL_IPS = ["10.0.0.5"]

    assert switch(client, users[0].pk, WIDGET, "10.0.0.5")[0] == 200


@pytest.mark.django_db
def test_errors(client: Client, users: list[User], settings: Settings) -> None:
    assert switch(client, 999_999, WIDGET) == (404, {"error": "no such user"})
    assert client.get(f"{API}/switch").status_code == 405
    settings.TRACEPANEL = {"user_switch": False}
    assert client.get(API).status_code == 404


def test_the_console_offers_switching_only_where_it_works(client: Client, settings: Settings) -> None:
    offered = client.get("/__tracepanel__/api/capabilities").json()
    settings.TRACEPANEL = {"user_switch": False}
    turned_off = client.get("/__tracepanel__/api/capabilities").json()

    assert offered == {"explain": True, "users": True, "profile": False, "request": True}
    assert turned_off == {"explain": True, "users": False, "profile": False, "request": True}


def test_the_console_says_whether_requests_are_recorded_in_detail(client: Client, settings: Settings) -> None:
    recorded = client.get("/__tracepanel__/api/capabilities").json()
    settings.TRACEPANEL = {"capture_request": False}
    not_recorded = client.get("/__tracepanel__/api/capabilities").json()

    assert recorded["request"] is True
    assert not_recorded["request"] is False


def test_custom_user_model() -> None:
    # AUTH_USER_MODEL is fixed per process: the custom-model tests run in their own pytest.
    result = subprocess.run(  # noqa: S603 (our own interpreter and test files)
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--ds=tests.project.settings_custom_user",
            str(Path(__file__).parent / "custom_user"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout[-3000:]


@pytest.mark.django_db
def test_switching_needs_debug_even_when_tracepanel_is_on(
    client: Client, settings: Settings, users: list[User]
) -> None:
    settings.DEBUG = False
    settings.TRACEPANEL = {"enabled": True}

    assert switch(client, users[0].pk, WIDGET) == (
        404,
        {"error": "user switching needs DEBUG, django.contrib.auth and sessions"},
    )
