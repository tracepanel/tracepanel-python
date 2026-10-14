from collections.abc import Generator
from typing import Any

import pytest
from dirty_equals import IsPartialDict

starlette = pytest.importorskip("starlette")

from starlette.applications import Starlette  # noqa: E402 (optional test group)
from starlette.requests import Request  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import tracepanel.asgi  # noqa: E402
from tests.agentstore import AgentStore, issues  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402


async def item(request: Request) -> JSONResponse:
    return JSONResponse({"id": request.path_params["item_id"]})


async def page(request: Request) -> HTMLResponse:
    del request
    return HTMLResponse("<html><head><title>t</title></head><body>hi</body></html>")


async def boom(request: Request) -> JSONResponse:
    del request
    message = "nope"
    raise ValueError(message)


@pytest.fixture
def restore() -> Generator[None, None, None]:
    previous = conf._framework  # noqa: SLF001
    yield
    # The Django tests go on with their own settings.
    conf.use_source(previous)


@pytest.fixture
def client(traces: list[Trace], restore: None) -> TestClient:
    app = Starlette(
        debug=True,
        routes=[
            Route("/items/{item_id:int}", item),
            Route("/page", page),
            Route("/boom", boom),
            Mount("/api", routes=[Route("/items/{item_id:int}", item)]),
        ],
    )

    assert tracepanel.asgi.install(app) is app

    return TestClient(app, raise_server_exceptions=False)


def views(trace: Trace) -> list[str]:
    return [span.name for span in trace.spans if span.kind == "view"]


def test_starlette_route_and_endpoint(client: TestClient, traces: list[Trace]) -> None:
    response = client.get("/items/7")
    (trace,) = traces

    assert (response.status_code, response.headers["Tracepanel-Id"]) == (200, trace.id)
    assert (trace.name, views(trace)) == ("GET /items/{item_id}", ["tests.asgi.test_asgi.item"])
    assert trace.attrs == IsPartialDict(
        {"http.route": "/items/{item_id}", "code.view": "tests.asgi.test_asgi.item", "http.status_code": 200}
    )


def test_routes_inside_a_mount(client: TestClient, traces: list[Trace]) -> None:
    response = client.get("/api/items/3")
    (trace,) = traces

    assert (trace.name, views(trace)) == ("GET /api/items/{item_id}", ["tests.asgi.test_asgi.item"])
    # The console is the app's, not under the mount the route is in.
    assert response.headers["Tracepanel-Url"] == f"http://testserver/__tracepanel__/#/t/{trace.id}"


def test_error_and_widget(client: TestClient, traces: list[Trace], store: AgentStore) -> None:
    error = client.get("/boom")
    html = client.get("/page").text

    assert (error.status_code, [e.type for e in traces[0].errors]) == (500, ["builtins.ValueError"])
    assert f'data-tracepanel-id="{traces[1].id}"' in html


def test_off_outside_debug_mode(traces: list[Trace], restore: None) -> None:
    app = Starlette(routes=[Route("/page", page)])

    TestClient(tracepanel.asgi.install(app)).get("/page")

    assert traces == []


async def bare(scope: dict[str, Any], receive: Any, send: Any) -> None:  # noqa: ANN401 (the ASGI interface)
    del scope, receive
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def test_any_asgi_app_is_wrapped(traces: list[Trace], restore: None, store: AgentStore) -> None:
    app = tracepanel.asgi.install(bare, enabled=True)
    response = TestClient(app).get("/orders/5")
    (trace,) = traces

    assert app is not bare
    assert (response.text, response.headers["Tracepanel-Id"]) == ("ok", trace.id)
    # Nothing in the scope names a route: the path does.
    assert (trace.name, views(trace)) == ("GET /orders/5", [])
    assert TestClient(app).get("/__tracepanel__/api/counts").status_code == 200


def test_one_trace_for_a_starlette_app_mounted_in_another(traces: list[Trace], restore: None) -> None:
    inner = Starlette(debug=True, routes=[Route("/items/{item_id:int}", item)])
    tracepanel.asgi.install(inner)
    outer = Starlette(debug=True, routes=[Mount("/v1", app=inner)])
    tracepanel.asgi.install(outer)

    TestClient(outer).get("/v1/items/3")
    (trace,) = traces

    assert (trace.name, views(trace)) == ("GET /v1/items/{item_id}", ["tests.asgi.test_asgi.item"])


def test_a_request_from_another_app_continues_its_trace(client: TestClient, traces: list[Trace]) -> None:
    client.get("/page", headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
    (trace,) = traces

    assert (trace.attrs["w3c.trace_id"], trace.attrs["w3c.parent_span_id"]) == (
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "00f067aa0ba902b7",
    )


def test_htmx_gets_a_whole_page(client: TestClient, traces: list[Trace]) -> None:
    client.get("/page", headers={"HX-Request": "true", "HX-Trigger": "load"})
    (trace,) = traces

    assert {key: value for key, value in trace.attrs.items() if key.startswith("htmx.")} == {
        "htmx.request": True,
        "htmx.trigger": "load",
        "htmx.full_page": True,
    }
    assert [issue.title for issue in issues(trace)] == ["Whole page sent to htmx · GET /page"]


def test_writes_through_the_console_come_from_this_machine_only(
    restore: None, store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tracepanel.asgi.install(bare, enabled=True)
    body = b"[]"
    headers = {"content-type": "application/json"}

    remote = TestClient(app, client=("10.0.0.5", 5000)).post(
        "/__tracepanel__/api/ingest", content=body, headers=headers
    )
    local = TestClient(app, client=("127.0.0.1", 5000)).post(
        "/__tracepanel__/api/ingest", content=body, headers=headers
    )

    assert (remote.status_code, local.status_code) == (403, 202)

    monkeypatch.setattr("tracepanel.asgi.middleware.MAX_BODY", 1)
    large = TestClient(app, client=("127.0.0.1", 5000)).post(
        "/__tracepanel__/api/ingest", content=body, headers=headers
    )

    assert (large.status_code, large.json()) == (413, {"error": "too large"})
