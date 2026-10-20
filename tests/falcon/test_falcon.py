from collections.abc import Generator

import pytest
from dirty_equals import IsPartialDict

pytest.importorskip("falcon")
strawberry = pytest.importorskip("strawberry")

import falcon  # noqa: E402
import falcon.testing  # noqa: E402

import tracepanel.falcon  # noqa: E402
from tests.agentstore import AgentStore  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402

App = falcon.App[falcon.Request, falcon.Response]


class PostResource:
    def on_get(self, req: falcon.Request, resp: falcon.Response, post_id: int) -> None:
        del req
        resp.media = {"id": post_id}

    def on_get_raw(self, req: falcon.Request, resp: falcon.Response, post_id: int) -> None:
        del req
        resp.text = str(post_id)

    def on_delete(self, req: falcon.Request, resp: falcon.Response, post_id: int) -> None:
        del req, resp, post_id
        message = "nope"
        raise ValueError(message)


class PageResource:
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        del req
        resp.content_type = falcon.MEDIA_HTML
        resp.text = "<html><head><title>t</title></head><body>hi</body></html>"


@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "world"


class GraphQLResource:
    """Strawberry has no Falcon integration: the responder executes the schema itself."""

    schema = strawberry.Schema(query=Query)

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        body = req.get_media()
        result = self.schema.execute_sync(body["query"], operation_name=body.get("operationName"))
        resp.media = {"data": result.data}


@pytest.fixture
def app(traces: list[Trace]) -> Generator[App, None, None]:
    previous = conf._framework  # noqa: SLF001
    application = falcon.App()
    application.add_route("/posts/{post_id:int}", PostResource())
    application.add_route("/page", PageResource())
    application.add_route("/posts/{post_id:int}/raw", PostResource(), suffix="raw")
    application.add_route("/graphql", GraphQLResource())
    tracepanel.falcon.install(application, enabled=True)
    yield application
    # The Django tests go on with their own settings.
    conf.use_source(previous)


@pytest.fixture
def client(app: App) -> falcon.testing.TestClient:
    # The app itself: install() works in place.
    return falcon.testing.TestClient(app)


def views(trace: Trace) -> list[str]:
    return [span.name for span in trace.spans if span.kind == "view"]


def test_route_and_responder(client: falcon.testing.TestClient, traces: list[Trace]) -> None:
    result = client.simulate_get("/posts/7")
    (trace,) = traces

    assert (result.json, result.headers["Tracepanel-Id"]) == ({"id": 7}, trace.id)
    assert (trace.name, views(trace)) == (
        "GET /posts/{post_id:int}",
        ["tests.falcon.test_falcon.PostResource.on_get"],
    )
    assert trace.attrs == IsPartialDict(
        {
            "http.route": "/posts/{post_id:int}",
            "code.view": "tests.falcon.test_falcon.PostResource.on_get",
            "http.status_code": 200,
        }
    )


def test_error_falcon_turns_into_a_500(client: falcon.testing.TestClient, traces: list[Trace]) -> None:
    result = client.simulate_delete("/posts/7")
    (trace,) = traces

    assert (result.status_code, result.headers["Tracepanel-Id"]) == (500, trace.id)
    assert (trace.status, views(trace)) == ("error", ["tests.falcon.test_falcon.PostResource.on_delete"])
    assert [(error.type, error.handled) for error in trace.errors] == [("builtins.ValueError", False)]


def test_http_errors_are_not_errors(client: falcon.testing.TestClient, traces: list[Trace]) -> None:
    result = client.simulate_get("/nowhere")
    (trace,) = traces

    assert (result.status_code, trace.name, trace.status, trace.errors) == (404, "GET /nowhere", "ok", [])


def test_errors_the_app_handles_are_not_errors(app: App, traces: list[Trace]) -> None:
    def unprocessable(req: falcon.Request, resp: falcon.Response, ex: Exception, params: object) -> None:
        del req, ex, params
        resp.status = falcon.HTTP_422

    app.add_error_handler(ValueError, unprocessable)
    result = falcon.testing.TestClient(app).simulate_delete("/posts/7")
    (trace,) = traces

    # The app's own handler answered: Falcon's default one (the 500) didn't run.
    assert (result.status_code, trace.status, trace.errors) == (422, "ok", [])


def test_strawberry_names_the_request(client: falcon.testing.TestClient, traces: list[Trace]) -> None:
    result = client.simulate_post("/graphql", json={"query": "query Greeting { hello }"})
    (trace,) = traces

    assert result.json == {"data": {"hello": "world"}}

    assert (trace.name, result.headers["Tracepanel-Name"]) == ("GraphQL query Greeting", "GraphQL query Greeting")

    assert trace.attrs == IsPartialDict(
        {
            "http.route": "/graphql",
            "code.view": "tests.falcon.test_falcon.GraphQLResource.on_post",
            "graphql.operation": "query Greeting",
        }
    )


def test_widget_in_html_pages(client: falcon.testing.TestClient, traces: list[Trace], store: AgentStore) -> None:
    body = client.simulate_get("/page").text

    assert f'data-tracepanel-id="{traces[0].id}"' in body


def test_console_is_served(client: falcon.testing.TestClient, store: AgentStore) -> None:
    assert client.simulate_get("/__tracepanel__/api/counts").status_code == 200


def test_other_apps_are_left_alone(app: App, traces: list[Trace]) -> None:
    other = falcon.App()
    other.add_route("/page", PageResource())

    falcon.testing.TestClient(other).simulate_get("/page")

    assert traces == []


def test_responder_with_a_suffix(client: falcon.testing.TestClient, traces: list[Trace]) -> None:
    client.simulate_get("/posts/7/raw")

    assert views(traces[0]) == ["tests.falcon.test_falcon.PostResource.on_get_raw"]
