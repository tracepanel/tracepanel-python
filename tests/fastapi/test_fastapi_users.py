from collections.abc import Generator

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("itsdangerous")

from fastapi import FastAPI, Request  # noqa: E402 (optional test group)
from fastapi.testclient import TestClient  # noqa: E402
from starlette.authentication import (  # noqa: E402
    AuthCredentials,
    AuthenticationBackend,
    BaseUser,
    SimpleUser,
)
from starlette.middleware.authentication import AuthenticationMiddleware  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402
from starlette.requests import HTTPConnection  # noqa: E402

import tracepanel.fastapi  # noqa: E402
from tracepanel._compat import override  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402


class Member(BaseUser):
    @property
    @override
    def is_authenticated(self) -> bool:
        return True

    @property
    @override
    def display_name(self) -> str:
        return "ann"

    @property
    @override
    def identity(self) -> str:
        return "7"


class ByHeader(AuthenticationBackend):
    """`Authorization: Member` signs in a user with an id; `Authorization: Simple` one with a name only."""

    @override
    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, BaseUser] | None:
        who = conn.headers.get("authorization")
        if who == "Member":
            return AuthCredentials(["authenticated"]), Member()
        if who == "Simple":
            return AuthCredentials(["authenticated"]), SimpleUser("bob")
        return None


def make_app() -> FastAPI:
    app = FastAPI(debug=True)
    app.add_middleware(AuthenticationMiddleware, backend=ByHeader())
    app.add_middleware(SessionMiddleware, secret_key="test")

    @app.post("/cart")
    def add(request: Request) -> dict[str, int]:
        request.session["cart"] = request.session.get("cart", 0) + 1
        request.session["csrf_token"] = "abc"
        return {"cart": request.session["cart"]}

    tracepanel.fastapi.install(app)
    return app


@pytest.fixture
def client(traces: list[Trace]) -> Generator[TestClient, None, None]:
    previous = conf._framework  # noqa: SLF001
    with TestClient(make_app()) as test_client:
        yield test_client
    conf.use_source(previous)


def who(trace: Trace) -> dict[str, object]:
    return {key: value for key, value in trace.attrs.items() if key.startswith(("session.", "user."))}


def test_the_session_and_the_signed_in_user_as_starlette_s_middleware_left_them(
    client: TestClient, traces: list[Trace]
) -> None:
    client.post("/cart", headers={"Authorization": "Member"})
    (trace,) = traces

    assert who(trace) == {
        "session.cart": "1",
        "session.csrf_token": "********",
        "user.id": "7",
        "user.username": "ann",
    }


def test_a_user_with_a_name_only_is_known_by_it(client: TestClient, traces: list[Trace]) -> None:
    client.post("/cart", headers={"Authorization": "Simple"})
    (trace,) = traces

    assert (trace.attrs["user.id"], trace.attrs["user.username"]) == ("bob", "bob")


def test_an_anonymous_visitor_has_no_id(client: TestClient, traces: list[Trace]) -> None:
    client.post("/cart")
    (trace,) = traces

    assert trace.attrs["user.id"] is None
    assert "user.username" not in trace.attrs


def test_without_starlette_s_middleware_there_is_neither(traces: list[Trace]) -> None:
    previous = conf._framework  # noqa: SLF001
    app = FastAPI(debug=True)

    @app.get("/plain")
    def plain() -> str:
        return "plain"

    tracepanel.fastapi.install(app)
    with TestClient(app) as test_client:
        test_client.get("/plain")
    conf.use_source(previous)
    (trace,) = traces

    assert who(trace) == {}
