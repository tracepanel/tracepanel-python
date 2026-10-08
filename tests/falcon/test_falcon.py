from collections.abc import Generator

import pytest
from dirty_equals import IsPartialDict

pytest.importorskip("falcon")

import falcon
import falcon.testing

import tracepanel.falcon
from tracepanel.core import conf
from tracepanel.core.model import Trace

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


@pytest.fixture
def app(traces: list[Trace]) -> Generator[App, None, None]:
    previous = conf._framework  # noqa: SLF001
    application = falcon.App()
    application.add_route("/posts/{post_id:int}", PostResource())
    application.add_route("/page", PageResource())
    application.add_route("/posts/{post_id:int}/raw", PostResource(), suffix="raw")
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


def test_widget_in_html_pages(client: falcon.testing.TestClient, traces: list[Trace]) -> None:
    body = client.simulate_get("/page").text

    assert f'data-tracepanel-id="{traces[0].id}"' in body


def test_console_is_served(client: falcon.testing.TestClient) -> None:
    assert client.simulate_get("/__tracepanel__/api/counts").status_code == 200


def test_other_apps_are_left_alone(app: App, traces: list[Trace]) -> None:
    other = falcon.App()
    other.add_route("/page", PageResource())

    falcon.testing.TestClient(other).simulate_get("/page")

    assert traces == []


def test_responder_with_a_suffix(client: falcon.testing.TestClient, traces: list[Trace]) -> None:
    client.simulate_get("/posts/7/raw")

    assert views(traces[0]) == ["tests.falcon.test_falcon.PostResource.on_get_raw"]
