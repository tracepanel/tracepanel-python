import asyncio
import time
from collections.abc import Generator
from importlib import metadata

import pytest
from dirty_equals import IsList, IsPartialDict, IsStr

flask = pytest.importorskip("flask")
flask_sqlalchemy = pytest.importorskip("flask_sqlalchemy")

import httpx  # noqa: E402
from asgiref.wsgi import WsgiToAsgi  # noqa: E402
from flask import Flask, render_template_string, request  # noqa: E402 (optional test group)
from flask.testing import FlaskClient  # noqa: E402
from flask_sqlalchemy import SQLAlchemy  # noqa: E402
from sqlalchemy import ForeignKey, select  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from werkzeug.debug import DebuggedApplication  # noqa: E402
from werkzeug.test import Client  # noqa: E402

import tracepanel.flask  # noqa: E402
import tracepanel.wsgi  # noqa: E402
from tests.agentstore import AgentStore, issues  # noqa: E402
from tests.profiles import share  # noqa: E402
from tracepanel.core import appinfo, conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


class Team(db.Model):  # type: ignore[name-defined]  # Flask-SQLAlchemy's dynamic base
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Player(db.Model):  # type: ignore[name-defined]
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"))
    team: Mapped[Team] = relationship()


def make_app() -> Flask:
    app = Flask(__name__)
    # install() turns tracepanel on in development only: debug mode (or TRACEPANEL_ENABLED=1).
    app.debug = True
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_ENGINE_OPTIONS={"poolclass": StaticPool, "connect_args": {"check_same_thread": False}},
        # The plain 500 page instead of the debugger, as in production.
        PROPAGATE_EXCEPTIONS=False,
    )
    db.init_app(app)

    @app.get("/players/<int:team_id>")
    def roster(team_id: int) -> dict[str, object]:
        # One query for the players, one more per player for its team: an N+1.
        names = [f"{player.name} ({player.team.name})" for player in db.session.scalars(select(Player))]
        return {"team": team_id, "players": names}

    @app.get("/busy")
    def busy() -> dict[str, int]:
        end, turns = time.perf_counter() + 0.15, 0
        while time.perf_counter() < end:
            turns += 1
        return {"turns": turns}

    @app.get("/boom")
    def boom() -> str:
        message = "nope"
        raise ValueError(message)

    @app.post("/signup")
    def signup() -> dict[str, object]:
        return {"fields": len(request.form)}

    @app.get("/page")
    def page() -> str:
        return "<html><head><title>t</title></head><body>hi</body></html>"

    @app.get("/rendered")
    def rendered() -> str:
        # The template reads player.team per player: the N+1 runs inside the template's span.
        players = db.session.scalars(select(Player)).all()
        return render_template_string("{% for p in players %}{{ p.team.name }} {% endfor %}", players=players)

    with app.app_context():
        db.create_all()
        for i in range(5):
            team, player = Team(), Player()
            team.name, player.name, player.team = f"team{i}", f"player{i}", team
            db.session.add(player)
        db.session.commit()
    tracepanel.flask.install(app)
    return app


@pytest.fixture
def app(traces: list[Trace]) -> Generator[Flask, None, None]:
    previous = conf._framework  # noqa: SLF001
    yield make_app()
    # The Django tests go on with their own settings.
    conf.use_source(previous)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_request_is_a_trace_named_after_its_rule(client: FlaskClient, traces: list[Trace]) -> None:
    response = client.get("/players/1")
    (trace,) = traces

    assert response.status_code == 200
    assert (trace.kind, trace.name) == ("http", "GET /players/<int:team_id>")
    assert trace.attrs == IsPartialDict(
        {
            "http.method": "GET",
            "http.path": "/players/1",
            "http.route": "/players/<int:team_id>",
            "http.status_code": 200,
            "code.view": "tests.flask.test_flask.make_app.<locals>.roster",
        }
    )

    (view,) = [span for span in trace.spans if span.kind == "view"]
    sql = [span for span in trace.spans if span.kind == "sql"]
    (issue,) = issues(trace)

    assert view.name == "tests.flask.test_flask.make_app.<locals>.roster"
    assert [span.name for span in sql] == ["SELECT player", *["SELECT team"] * 5]
    assert (issue.kind, issue.count) == ("n+1", 5)
    assert issue.hint == IsStr(regex=r".*`joinedload\(\)`.*")

    assert response.headers["Tracepanel-Id"] == trace.id
    assert response.headers["Server-Timing"] == IsStr(
        regex=r'db;dur=[\d.]+;desc="6 queries".*tracepanel-issues;desc="pending"'
    )


def test_under_an_asgi_server_the_profile_is_the_views(
    app: Flask, traces: list[Trace], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()

    async def get() -> httpx.Response:
        # asgiref runs the WSGI app in a worker thread, where its trace starts: the event loop stays out of it.
        transport = httpx.ASGITransport(app=WsgiToAsgi(app))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/busy", headers={"X-Tracepanel-Profile": "1"})

    response = asyncio.run(get())
    (trace,) = traces

    assert response.status_code == 200
    assert share(trace, "make_app.<locals>.busy") > 0.3


def test_unhandled_error(client: FlaskClient, traces: list[Trace]) -> None:
    response = client.get("/boom")
    (trace,) = traces

    assert (response.status_code, response.headers["Tracepanel-Id"]) == (500, trace.id)
    assert (trace.status, trace.attrs["http.status_code"]) == ("error", 500)
    assert [(error.type, error.handled) for error in trace.errors] == [("builtins.ValueError", False)]


def test_error_through_to_the_debugger(app: Flask, traces: list[Trace]) -> None:
    app.config["PROPAGATE_EXCEPTIONS"] = True

    with pytest.raises(ValueError, match="nope"):
        app.test_client().get("/boom")

    (trace,) = traces
    assert (trace.status, trace.attrs["http.status_code"]) == ("error", 500)
    assert [error.type for error in trace.errors] == ["builtins.ValueError"]


def test_debugger_page_carries_the_trace(app: Flask, traces: list[Trace]) -> None:
    app.config["PROPAGATE_EXCEPTIONS"] = True
    client = Client(DebuggedApplication(app.wsgi_app))

    response = client.get("/boom")
    (trace,) = traces

    assert (response.status_code, response.headers["Tracepanel-Id"]) == (500, trace.id)
    assert response.headers["Tracepanel-Url"] == f"http://localhost/__tracepanel__/#/t/{trace.id}"


def test_unknown_url_keeps_its_path(client: FlaskClient, traces: list[Trace]) -> None:
    client.get("/nowhere")
    (trace,) = traces

    assert (trace.name, trace.attrs["http.status_code"]) == ("GET /nowhere", 404)


def test_widget_in_html_pages(client: FlaskClient, traces: list[Trace], store: AgentStore) -> None:
    response = client.get("/page")
    body = response.get_data(as_text=True)

    assert body.index("<head>") < body.index('<script src="/__tracepanel__/widget.min.js"') < body.index("<title>")
    assert f'data-tracepanel-id="{traces[0].id}"' in body
    assert response.headers["Content-Length"] == str(len(response.get_data()))


def test_console_is_served(client: FlaskClient, store: AgentStore) -> None:
    assert client.get("/__tracepanel__/api/counts").json == IsPartialDict(sections=IsPartialDict(http=0))


def test_explain_runs_on_the_engine(client: FlaskClient, traces: list[Trace], store: AgentStore) -> None:
    client.get("/players/1")
    store.write(traces)
    (trace,) = traces
    first = next(span for span in trace.spans if span.kind == "sql")

    plan = client.get("/__tracepanel__/api/explain", query_string={"trace": trace.id, "span": first.id}).json

    assert plan == IsPartialDict(
        vendor="sqlite", analyzed=False, lines=[IsPartialDict(text=IsStr(regex=r"SCAN player.*"))]
    )


def test_off_outside_debug_mode(traces: list[Trace]) -> None:
    previous = conf._framework  # noqa: SLF001
    app = Flask(__name__)
    app.get("/")(lambda: "ok")
    tracepanel.flask.install(app)

    app.test_client().get("/")

    conf.use_source(previous)
    assert traces == []


def test_templates_are_spans(client: FlaskClient, traces: list[Trace]) -> None:
    client.get("/rendered")
    (trace,) = traces
    (view,) = [span for span in trace.spans if span.kind == "view"]
    (transaction,) = [span for span in trace.spans if span.kind == "transaction"]
    (template,) = [span for span in trace.spans if span.kind == "template"]
    in_template = [span.name for span in trace.spans if span.kind == "sql" and span.parent_id == template.id]

    assert (template.name, template.attrs["template.name"]) == ("<string template>", "<string template>")
    # The session's transaction, opened by the view's first query, is still open while the template renders.
    assert (template.parent_id, transaction.parent_id) == (transaction.id, view.id)
    assert in_template == ["SELECT team"] * 5


def test_one_trace_when_mounted_in_another_wsgi_app(app: Flask, traces: list[Trace]) -> None:
    # The Flask app behind a generic tracepanel.wsgi wrapper: the outer layer traces, Flask still names the request.
    outer = tracepanel.wsgi.install(app, enabled=True)

    response = Client(outer).get("/players/1")
    (trace,) = traces

    assert (response.status_code, trace.name) == (200, "GET /players/<int:team_id>")
    assert [span.kind for span in trace.spans if span.kind == "view"] == ["view"]


def test_a_form_the_view_read_is_recorded_masked(client: FlaskClient, traces: list[Trace]) -> None:
    client.post(
        "/signup?next=/home", data={"email": "ann@example.com", "password": "p"}, headers={"X-Request-Id": "r-1"}
    )

    (trace,) = traces
    assert (
        trace.attrs["http.request.header.x_request_id"],
        trace.attrs["url.query"],
        trace.attrs["http.request.body"],
    ) == ("r-1", "next=/home", "email=ann@example.com&password=********")


def test_a_template_keeps_the_context_flask_gave_it(client: FlaskClient, traces: list[Trace]) -> None:
    client.get("/rendered")
    (trace,) = traces

    (template,) = [span for span in trace.spans if span.kind == "template"]

    assert template.attrs == {
        "template.name": "<string template>",
        # Flask's context processor adds g and the request; config is a global of the environment's, not context.
        "template.context.names": ["players", "g", "request"],
        "template.context.values": [
            # Flask-SQLAlchemy's repr: no attribute loaded for it (each player's team loads in the template).
            "[<Player 1>, <Player 2>, <Player 3>, <Player 4>, <Player 5>]",
            "<flask.g of 'tests.flask.test_flask'>",
            "<Request 'http://localhost/rendered' [GET]>",
        ],
    }


def test_the_app_tab_shows_app_config_masked_and_flasks_version(app: Flask) -> None:
    app.config["MAIL_PASSWORD"] = "pw"
    app.config["REDIS_URL"] = "redis://:pw@cache:6379/0"

    described = appinfo.describe(conf.get_config())

    assert described["settings"] == IsPartialDict(
        {
            "DEBUG": "True",
            "SECRET_KEY": "********",
            "MAIL_PASSWORD": "********",
            "REDIS_URL": "'redis://********@cache:6379/0'",
            "SQLALCHEMY_DATABASE_URI": "'sqlite://'",
            "PROPAGATE_EXCEPTIONS": "False",
        }
    )

    assert described["versions"] == IsList(
        IsPartialDict({"kind": "runtime"}),
        {"name": "Flask", "version": metadata.version("flask"), "kind": "framework"},
        length=...,
    )
