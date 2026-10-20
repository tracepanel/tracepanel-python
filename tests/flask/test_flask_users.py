from collections.abc import Generator

import pytest

flask = pytest.importorskip("flask")
flask_login = pytest.importorskip("flask_login")

from flask import Flask, redirect, session  # noqa: E402 (optional test group)
from flask.testing import FlaskClient  # noqa: E402
from flask_login import LoginManager, UserMixin, current_user, login_user  # noqa: E402
from werkzeug.wrappers import Response  # noqa: E402

import tracepanel.flask  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402


class Member(UserMixin):
    def __init__(self, member_id: str, username: str) -> None:
        self.id = member_id
        self.username = username


MEMBERS = {"7": Member("7", "ann")}


def make_app() -> Flask:
    app = Flask(__name__)
    app.debug = True
    app.secret_key = "test"
    logins = LoginManager(app)
    logins.user_loader(MEMBERS.get)

    @app.post("/login")
    def login() -> Response:
        login_user(MEMBERS["7"])
        session["cart"] = 3
        return redirect("/me")

    @app.get("/me")
    def me() -> dict[str, object]:
        return {"name": current_user.username if current_user.is_authenticated else None}

    @app.get("/plain")
    def plain() -> str:
        return "no session, no user"

    tracepanel.flask.install(app)
    return app


@pytest.fixture
def client(traces: list[Trace]) -> Generator[FlaskClient, None, None]:
    previous = conf._framework  # noqa: SLF001
    yield make_app().test_client()
    conf.use_source(previous)


def test_the_session_the_view_left_and_the_user_flask_login_loaded(client: FlaskClient, traces: list[Trace]) -> None:
    client.post("/login")
    client.get("/me")
    signed_in, me = traces

    assert {key: value for key, value in signed_in.attrs.items() if key.startswith(("session.", "user."))} == {
        "session._user_id": "'7'",
        "session._fresh": "True",
        "session._id": "********",
        "session.cart": "3",
        "user.id": "7",
        "user.username": "ann",
    }

    assert me.attrs["user.id"] == "7"
    assert me.attrs["user.username"] == "ann"


def test_an_anonymous_visitor_has_no_id(client: FlaskClient, traces: list[Trace]) -> None:
    client.get("/me")
    (trace,) = traces

    assert trace.attrs["user.id"] is None
    assert "user.username" not in trace.attrs


def test_a_view_that_never_asked_for_the_user_records_the_session_only(
    client: FlaskClient, traces: list[Trace]
) -> None:
    client.post("/login")
    client.get("/plain")
    _, plain = traces

    assert plain.attrs["session.cart"] == "3"
    assert [key for key in plain.attrs if key.startswith("user.")] == []
