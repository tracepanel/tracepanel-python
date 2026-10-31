import threading
from collections.abc import Generator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any

import pytest
from dirty_equals import IsInt, IsPartialDict, IsStr

fastapi = pytest.importorskip("fastapi")
sqlalchemy = pytest.importorskip("sqlalchemy")
strawberry = pytest.importorskip("strawberry")

import httpx  # noqa: E402 (optional test group)
from fastapi import APIRouter, Depends, FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sqlalchemy import ForeignKey, create_engine, select, text  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from strawberry.fastapi import GraphQLRouter  # noqa: E402

import tracepanel.fastapi  # noqa: E402
from tests.agentstore import (  # noqa: E402
    OTHER_APPS_PLAN,
    OTHER_APPS_ROWS,
    AgentStore,
    issues,
    other_services_query,
)
from tests.profiles import sampled, share_meanwhile  # noqa: E402
from tracepanel._compat import override  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402


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


def spin(seconds: float) -> int:
    """Busy in project code for a while, sampled: what a profile shows."""
    return sampled(seconds)


class Account(BaseModel):
    email: str
    password: str


def make_app() -> FastAPI:
    # install() turns tracepanel on in development only: debug=True (or TRACEPANEL_ENABLED=1).
    app = FastAPI(debug=True)

    @app.get("/players/{team_id}")
    def roster(team_id: int) -> dict[str, object]:
        return {"team": team_id, "players": players()}

    @app.get("/busy")
    def busy() -> dict[str, int]:
        return {"turns": spin(0.15)}

    @app.get("/boom")
    def boom() -> None:
        message = "nope"
        raise ValueError(message)

    @app.post("/signup")
    def signup(account: Account) -> dict[str, str]:
        return {"email": account.email}

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
    (issue,) = issues(trace)
    assert (issue.kind, issue.count) == ("n+1", 5)
    # The fix in SQLAlchemy's words, not Django's select_related().
    assert issue.hint == IsStr(regex=r".*`joinedload\(\)`.*`selectinload\(\)`.*")

    assert response.headers["Tracepanel-Id"] == trace.id
    assert response.headers["Server-Timing"] == IsStr(
        regex=r'db;dur=[\d.]+;desc="6 queries".*tracepanel-issues;desc="pending"'
    )


def test_a_sync_endpoint_is_profiled_in_the_thread_that_runs_it(
    client: TestClient, traces: list[Trace], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    conf.reset()

    response = client.get("/busy", headers={"X-Tracepanel-Profile": "1"})
    (trace,) = traces

    assert response.status_code == 200
    assert trace.profile is not None
    in_app = {frame["function"] for frame in trace.profile["frames"] if frame["in_app"]}  # type: ignore[union-attr]
    assert {"make_app.<locals>.busy", "spin"} <= in_app
    # The thread the endpoint runs in, not the event loop's, which waits in its selector meanwhile.
    assert share_meanwhile(trace, "spin") == 1

    assert trace.spans[0].attrs["profile.reason"] == "header"


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


def test_widget_in_html_pages(client: TestClient, traces: list[Trace], store: AgentStore) -> None:
    body = client.get("/page").text

    assert body.index("<head>") < body.index('<script src="/__tracepanel__/widget.min.js"') < body.index("<title>")
    assert f'data-tracepanel-id="{traces[0].id}"' in body


def test_strawberry_names_the_request(client: TestClient, traces: list[Trace]) -> None:
    response = client.post("/graphql", json={"query": "query Roster { roster }"})
    (trace,) = traces

    assert response.json()["data"]["roster"][0] == "player0 (team0)"
    assert trace.name == "GraphQL query Roster"
    assert response.headers["Tracepanel-Name"] == "GraphQL query Roster"
    assert [issue.kind for issue in issues(trace)] == ["n+1"]


def test_console_is_served(client: TestClient, store: AgentStore) -> None:
    assert client.get("/__tracepanel__/api/counts").json() == IsPartialDict(sections=IsPartialDict(http=0))


def test_explain_runs_on_the_engine(client: TestClient, traces: list[Trace], store: AgentStore) -> None:
    client.get("/players/1")
    store.write(traces)
    (trace,) = traces
    first = next(span for span in trace.spans if span.kind == "sql")

    plan = client.get("/__tracepanel__/api/explain", params={"trace": trace.id, "span": first.id}).json()

    # Unnamed engines are numbered in the order they appear: other tests may have made one first.
    assert plan == IsPartialDict(vendor="sqlite", alias=IsStr(regex=r"sqlite(#\d+)?"), analyzed=False)
    assert plan["lines"] == [IsPartialDict(text=IsStr(regex=r"SCAN player.*"))]


def test_explain_of_another_services_trace_is_the_agents(client: TestClient, store: AgentStore) -> None:
    # A linked trace of a call chain, recorded by a JavaScript BFF: its database isn't this app's.
    trace, span = other_services_query("bff")
    store.write([trace])

    with store.other_app("bff", OTHER_APPS_ROWS) as asked:
        response = client.get("/__tracepanel__/api/explain", params={"trace": trace.id, "span": span})

    assert response.status_code == 200
    assert response.json() == OTHER_APPS_PLAN

    assert asked == [IsPartialDict(id=IsInt, explain=IsPartialDict(sql="EXPLAIN QUERY PLAN SELECT * FROM orders"))]


def test_routes_of_included_routers_keep_their_prefix(client: TestClient, traces: list[Trace]) -> None:
    client.get("/teams/7/players")
    client.post("/graphql", json={"query": "{ roster }"})

    assert [trace.attrs["http.route"] for trace in traces] == ["/teams/{team_id}/players", "/graphql"]


def test_routes_of_included_routers_run_in_a_view(client: TestClient, traces: list[Trace]) -> None:
    client.get("/teams/7/players")
    (trace,) = traces

    assert [(span.kind, span.name) for span in trace.spans] == [
        ("http", "GET /teams/{team_id}/players"),
        ("view", IsStr(regex=r".*make_app\.<locals>\.team_players")),
    ]


class Pong(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"pong")

    @override
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 (http.server's signature)
        pass


@pytest.fixture(scope="module")
def pong() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Pong)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/ping"
    httpd.shutdown()
    httpd.server_close()


def test_a_transaction_opened_in_a_dependency_holds_the_endpoint_calls(traces: list[Trace], pong: str) -> None:
    app = FastAPI(debug=True)

    # Sync dependencies and endpoints: FastAPI runs each one in a worker thread, in a copy of the request's context.
    def get_db() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    def current_user(db: Annotated[Session, Depends(get_db)]) -> int:
        # The session's transaction begins here.
        return int(db.execute(text("SELECT 1")).scalar_one())

    @app.post("/pay")
    def pay(db: Annotated[Session, Depends(get_db)], user: Annotated[int, Depends(current_user)]) -> int:
        httpx.get(pong)
        db.execute(text("SELECT 2"))
        db.commit()
        return user

    previous = conf._framework  # noqa: SLF001
    tracepanel.fastapi.install(app)
    with TestClient(app) as test_client:
        test_client.post("/pay")
    # The Django tests go on with their own settings.
    conf.use_source(previous)
    (trace,) = traces
    by_id = {span.id: span for span in trace.spans}

    # Everything the request did while the transaction was open is inside it, the call to another service included.
    assert [(span.kind, span.name, by_id[span.parent_id].kind) for span in trace.spans if span.parent_id] == [
        ("view", IsStr(regex=r".*<locals>\.pay"), "http"),
        ("transaction", IsStr(regex=r"transaction sqlite(#\d+)?"), "view"),
        ("sql", "SELECT", "transaction"),
        ("http.client", "GET 127.0.0.1/ping", "transaction"),
        ("sql", "SELECT", "transaction"),
    ]

    assert [issue.kind for issue in issues(trace)] == ["in-transaction"]


@pytest.mark.django_db(transaction=True)
def test_a_django_atomic_block_around_the_endpoint_holds_its_calls(traces: list[Trace], pong: str) -> None:
    from django.db import connection, transaction  # noqa: PLC0415

    app = FastAPI(debug=True)

    # The block opens in one worker thread's copy of the request's context, and closes in another's.
    def atomic() -> Iterator[None]:
        with transaction.atomic():
            yield

    @app.post("/pay", dependencies=[Depends(atomic)])
    def pay() -> None:
        httpx.get(pong)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 2")

    previous = conf._framework  # noqa: SLF001
    tracepanel.fastapi.install(app)
    with TestClient(app) as test_client:
        test_client.post("/pay")
    conf.use_source(previous)
    (trace,) = traces
    by_id = {span.id: span for span in trace.spans}
    (block,) = [span for span in trace.spans if span.kind == "transaction"]
    # SQLite's backend sends BEGIN as a statement; PostgreSQL's driver begins on its own, on a connection the worker
    # thread opens first (a `sql.connect` span).
    calls = [span for span in trace.spans if span.name != "BEGIN" and span.kind != "sql.connect"]

    assert [(span.kind, span.name, by_id[span.parent_id].kind) for span in calls if span.parent_id] == [
        ("view", IsStr(regex=r".*<locals>\.pay"), "http"),
        ("transaction", "transaction default", "view"),
        ("http.client", "GET 127.0.0.1/ping", "transaction"),
        ("sql", "SELECT", "transaction"),
    ]

    # It ended where Django committed it, in another thread than the one it began in.
    assert (block.attrs["db.transaction.outcome"], block.duration > 0) == ("commit", True)

    assert [issue.kind for issue in issues(trace)] == ["in-transaction"]


def test_an_async_session_in_an_async_endpoint_names_the_endpoint_and_explains(
    traces: list[Trace], store: AgentStore, tmp_path: Path
) -> None:
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")  # SQLAlchemy's asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shop.sqlite3'}")
    app = FastAPI(debug=True)

    @app.get("/tables")
    async def tables() -> int:
        async with AsyncSession(async_engine) as db:
            result = await db.execute(text("SELECT name FROM sqlite_master WHERE type = :type"), {"type": "table"})
            return len(result.all())

    previous = conf._framework  # noqa: SLF001
    tracepanel.fastapi.install(app)
    with TestClient(app) as test_client:
        test_client.get("/tables")
        store.write(traces)
        (trace,) = traces
        _, _, transaction, statement = trace.spans
        plan = test_client.get("/__tracepanel__/api/explain", params={"trace": trace.id, "span": statement.id})
    conf.use_source(previous)

    in_tables = IsStr(regex=r"tests/fastapi/test_fastapi\.py:\d+ in tables")
    assert [(span.kind, span.attrs["code.origin"]) for span in (transaction, statement)] == [
        ("transaction", in_tables),
        ("sql", in_tables),
    ]

    assert plan.json() == {
        "vendor": "sqlite",
        "alias": "shop.sqlite3",
        "analyzed": False,
        "lines": [
            {"depth": 0, "text": "SCAN sqlite_master", "warning": "Full scan of sqlite_master: no index is used"}
        ],
    }


def test_a_body_the_endpoint_read_is_recorded_masked(client: TestClient, traces: list[Trace]) -> None:
    client.post("/signup", json={"email": "ann@example.com", "password": "p"}, headers={"X-Request-Id": "r-1"})

    (trace,) = traces
    assert (trace.attrs["http.request.header.x_request_id"], trace.attrs["http.request.body"]) == (
        "r-1",
        '{"email":"ann@example.com","password":"********"}',
    )


def test_the_response_body_is_recorded(client: TestClient, traces: list[Trace]) -> None:
    client.post("/signup", json={"email": "ann@example.com", "password": "p"})

    (trace,) = traces
    assert {key: value for key, value in trace.attrs.items() if key.startswith("http.response.body")} == {
        "http.response.body": '{"email":"ann@example.com"}',
        "http.response.body.size": 27,
    }
