from collections.abc import Generator

import pytest
from dirty_equals import IsPartialDict, IsStr

flask = pytest.importorskip("flask")
flask_sqlalchemy = pytest.importorskip("flask_sqlalchemy")

from flask import Flask, render_template_string  # noqa: E402 (optional test group)
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
from tracepanel.core import conf  # noqa: E402
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

    @app.get("/boom")
    def boom() -> str:
        message = "nope"
        raise ValueError(message)

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
    # Flask can't switch the user: the panel doesn't ask.
    assert "data-tracepanel-users" not in body
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

    assert (template.name, template.attrs) == ("<string template>", {"template.name": "<string template>"})
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
