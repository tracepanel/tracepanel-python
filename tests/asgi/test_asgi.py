import time
from collections.abc import AsyncIterator, Generator
from typing import Any

import pytest
from dirty_equals import IsInt, IsPartialDict

starlette = pytest.importorskip("starlette")

from starlette.applications import Starlette  # noqa: E402 (optional test group)
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import tracepanel.asgi  # noqa: E402
from tests.agentstore import AgentStore, issues  # noqa: E402
from tracepanel._compat import override  # noqa: E402
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


class Logging(BaseHTTPMiddleware):
    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        return await call_next(request)


def test_widget_through_a_base_http_middleware(traces: list[Trace], restore: None, store: AgentStore) -> None:
    # BaseHTTPMiddleware sends every response in several messages: one with a Content-Length is still whole.
    app = Starlette(debug=True, routes=[Route("/page", page)], middleware=[Middleware(Logging)])
    tracepanel.asgi.install(app)

    response = TestClient(app).get("/page")
    (trace,) = traces

    assert f'data-tracepanel-id="{trace.id}"' in response.text
    assert response.headers["Content-Length"] == str(len(response.content))
    assert response.headers["Tracepanel-Url"] == f"http://testserver/__tracepanel__/#/t/{trace.id}"
    assert "total;dur=" in response.headers["Server-Timing"]


async def chunks() -> AsyncIterator[str]:
    yield "<html><head></head><body>"
    yield "streamed</body></html>"


async def streamed(request: Request) -> StreamingResponse:
    del request
    return StreamingResponse(chunks(), media_type="text/html")


def test_a_streamed_page_goes_out_as_it_is_made(
    traces: list[Trace], restore: None, store: AgentStore, capsys: pytest.CaptureFixture[str]
) -> None:
    app = Starlette(debug=True, routes=[Route("/streamed", streamed)])
    tracepanel.asgi.install(app)

    response = TestClient(app).get("/streamed")
    (trace,) = traces

    assert response.text == "<html><head></head><body>streamed</body></html>"
    assert (response.headers["Tracepanel-Id"], "Tracepanel-Url" in response.headers) == (trace.id, False)

    # Said in the terminal: a page without the widget looks like tracepanel being off.
    assert capsys.readouterr().err == (
        "tracepanel ▸ GET /streamed: an HTML page streamed (it has no Content-Length), so without the widget\n"
    )


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


async def echo(request: Request) -> JSONResponse:
    return JSONResponse({"read": len(await request.body())})


async def ignores(request: Request) -> JSONResponse:
    del request
    return JSONResponse({"read": 0})


@pytest.fixture
def bodies(traces: list[Trace], restore: None) -> TestClient:
    app = Starlette(
        debug=True, routes=[Route("/echo", echo, methods=["POST"]), Route("/ignore", ignores, methods=["POST"])]
    )
    tracepanel.asgi.install(app)
    return TestClient(app)


def carried(trace: Trace) -> dict[str, object]:
    return {
        key: value
        for key, value in trace.attrs.items()
        if key.startswith(("http.request.cookie.", "http.request.body", "url."))
        or key.endswith(("x_request_id", "set_cookie"))
    }


def test_a_request_records_what_it_carried_with_secrets_masked(bodies: TestClient, traces: list[Trace]) -> None:
    bodies.cookies.set("theme", "dark")
    bodies.cookies.set("sessionid", "s-1")

    bodies.post(
        "/echo?page=2&token=abc",
        content=b'{"email": "ann@example.com", "password": "p"}',
        headers={"Content-Type": "application/json", "X-Request-Id": "r-1"},
    )

    (trace,) = traces
    assert carried(trace) == {
        "http.request.header.x_request_id": "r-1",
        "url.query": "page=2&token=********",
        "http.request.cookie.theme": "dark",
        "http.request.cookie.sessionid": "********",
        "http.request.body": '{"email":"ann@example.com","password":"********"}',
    }

    assert trace.attrs["http.response.header.content_type"] == "application/json"


def test_a_body_the_endpoint_never_read_is_not_read_for_it(bodies: TestClient, traces: list[Trace]) -> None:
    bodies.post("/ignore", content=b'{"password": "p"}', headers={"Content-Type": "application/json"})

    (trace,) = traces
    assert "http.request.body" not in trace.attrs


def test_nothing_is_recorded_with_capture_request_off(
    bodies: TestClient, traces: list[Trace], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_CAPTURE_REQUEST", "0")
    conf.reset()

    bodies.post("/echo?page=2", content=b'{"a": 1}', headers={"Content-Type": "application/json", "X-Request-Id": "r"})

    (trace,) = traces
    assert carried(trace) == {}
    assert not [key for key in trace.attrs if key.startswith("http.response.header.")]


async def busy(request: Request) -> JSONResponse:
    del request
    # Blocking the loop on purpose: CPU time in the request's own code is what a profile shows.
    end = time.perf_counter() + 0.15
    while time.perf_counter() < end:
        pass
    return JSONResponse({"done": True})


def test_the_panels_cookie_profiles_each_request_while_it_is_set(
    traces: list[Trace], restore: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_PROFILE_INTERVAL_MS", "1")
    app = Starlette(debug=True, routes=[Route("/busy", busy)])
    tracepanel.asgi.install(app)
    client = TestClient(app)

    profiled_response = client.get("/busy", headers={"Cookie": "tracepanel_profile=1"})
    plain = client.get("/busy")
    profiled, unprofiled = traces

    assert profiled.profile is not None
    assert profiled.profile["samples"] == IsInt(gt=0)
    assert "set-cookie" not in profiled_response.headers

    assert unprofiled.profile is None
    assert "set-cookie" not in plain.headers
