from collections.abc import Generator

import pytest
from dirty_equals import IsPartialDict, IsStr

fastapi = pytest.importorskip("fastapi")
sqlalchemy = pytest.importorskip("sqlalchemy")
strawberry = pytest.importorskip("strawberry")

from fastapi import APIRouter, FastAPI  # noqa: E402 (optional test group)
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import ForeignKey, create_engine, select  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from strawberry.fastapi import GraphQLRouter  # noqa: E402

import tracepanel.fastapi  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402
from tracepanel.storage.store import Store  # noqa: E402


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "team"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Player(Base):
    __tablename__ = "player"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"))
    team: Mapped[Team] = relationship()


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
with Session(engine) as session:
    teams = [Team(name=f"team{i}") for i in range(5)]
    session.add_all([Player(name=f"player{i}", team=team) for i, team in enumerate(teams)])
    session.commit()


def players() -> list[str]:
    with Session(engine) as session:
        # One query for the players, one more per player for its team: an N+1.
        return [f"{player.name} ({player.team.name})" for player in session.scalars(select(Player))]


@strawberry.type
class Query:
    @strawberry.field
    def roster(self) -> list[str]:
        return players()


def make_app() -> FastAPI:
    # install() turns tracepanel on in development only: debug=True (or TRACEPANEL_ENABLED=1).
    app = FastAPI(debug=True)

    @app.get("/players/{team_id}")
    def roster(team_id: int) -> dict[str, object]:
        return {"team": team_id, "players": players()}

    @app.get("/boom")
    def boom() -> None:
        message = "nope"
        raise ValueError(message)

    @app.get("/page", response_class=HTMLResponse)
    def page() -> str:
        return "<html><head><title>t</title></head><body>hi</body></html>"

    app.include_router(GraphQLRouter(strawberry.Schema(query=Query)), prefix="/graphql")
    teams = APIRouter()

    @teams.get("/{team_id}/players")
    def team_players(team_id: int) -> dict[str, int]:
        return {"team": team_id}

    app.include_router(teams, prefix="/teams")
    tracepanel.fastapi.install(app)
    return app


@pytest.fixture
def client(traces: list[Trace]) -> Generator[TestClient, None, None]:
    previous = conf._framework  # noqa: SLF001
    with TestClient(make_app(), raise_server_exceptions=False) as test_client:
        yield test_client
    # The Django tests go on with their own settings.
    conf.use_source(previous)


def test_request_is_a_trace_named_after_its_route(client: TestClient, traces: list[Trace]) -> None:
    response = client.get("/players/1")
    (trace,) = traces

    assert response.status_code == 200
    assert (trace.kind, trace.name) == ("http", "GET /players/{team_id}")
    assert trace.attrs == IsPartialDict(
        {
            "http.method": "GET",
            "http.path": "/players/1",
            "http.route": "/players/{team_id}",
            "http.status_code": 200,
            "code.view": IsStr(regex=r".*make_app\.<locals>\.roster"),
        }
    )

    (view,) = [span for span in trace.spans if span.kind == "view"]
    assert view.name == IsStr(regex=r".*make_app\.<locals>\.roster")

    sql = [span for span in trace.spans if span.kind == "sql"]
    assert [span.name for span in sql] == ["SELECT player", *["SELECT team"] * 5]
    # The session's transaction (opened by its first query, rolled back as it closes) holds the queries.
    (transaction,) = [span for span in trace.spans if span.kind == "transaction"]
    assert (transaction.parent_id, transaction.attrs["db.transaction.outcome"]) == (view.id, "rollback")
    assert {span.parent_id for span in sql} == {transaction.id}
    (issue,) = trace.issues
    assert (issue.kind, issue.count) == ("n+1", 5)
    # The fix in SQLAlchemy's words, not Django's select_related().
    assert issue.hint == IsStr(regex=r".*`joinedload\(\)`.*`selectinload\(\)`.*")

    assert response.headers["Tracepanel-Id"] == trace.id
    assert response.headers["Server-Timing"] == IsStr(
        regex=r'db;dur=[\d.]+;desc="6 queries".*tracepanel-issues;desc="1"'
    )


def test_unhandled_error(client: TestClient, traces: list[Trace]) -> None:
    response = client.get("/boom")
    (trace,) = traces

    # The server's plain 500, with the headers that let the widget open the failed request.
    assert (response.status_code, response.text, response.headers["Tracepanel-Id"]) == (
        500,
        "Internal Server Error",
        trace.id,
    )

    assert (trace.status, trace.attrs["http.status_code"]) == ("error", 500)
    assert [error.type for error in trace.errors] == ["builtins.ValueError"]


def test_widget_in_html_pages(client: TestClient, traces: list[Trace]) -> None:
    body = client.get("/page").text

    assert body.index("<head>") < body.index('<script src="/__tracepanel__/widget.js"') < body.index("<title>")
    assert f'data-tracepanel-id="{traces[0].id}"' in body


def test_strawberry_names_the_request(client: TestClient, traces: list[Trace]) -> None:
    response = client.post("/graphql", json={"query": "query Roster { roster }"})
    (trace,) = traces

    assert response.json()["data"]["roster"][0] == "player0 (team0)"
    assert trace.name == "GraphQL query Roster"
    assert response.headers["Tracepanel-Name"] == "GraphQL query Roster"
    assert [issue.kind for issue in trace.issues] == ["n+1"]


def test_console_is_served(client: TestClient, store: Store) -> None:
    assert client.get("/__tracepanel__/api/counts").json() == IsPartialDict(sections=IsPartialDict(http=0))


def test_explain_runs_on_the_engine(client: TestClient, traces: list[Trace], store: Store) -> None:
    client.get("/players/1")
    store.write(traces)
    (trace,) = traces
    first = next(span for span in trace.spans if span.kind == "sql")

    plan = client.get("/__tracepanel__/api/explain", params={"trace": trace.id, "span": first.id}).json()

    # Unnamed engines are numbered in the order they appear: other tests may have made one first.
    assert plan == IsPartialDict(vendor="sqlite", alias=IsStr(regex=r"sqlite(#\d+)?"), analyzed=False)
    assert plan["lines"] == [IsPartialDict(text=IsStr(regex=r"SCAN player.*"))]


def test_routes_of_included_routers_keep_their_prefix(client: TestClient, traces: list[Trace]) -> None:
    client.get("/teams/7/players")
    client.post("/graphql", json={"query": "{ roster }"})

    assert [trace.attrs["http.route"] for trace in traces] == ["/teams/{team_id}/players", "/graphql"]
