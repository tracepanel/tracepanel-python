from collections.abc import Generator

import pytest

fastapi = pytest.importorskip("fastapi")
jinja2 = pytest.importorskip("jinja2")

from fastapi import FastAPI, Request  # noqa: E402 (optional test group)
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import tracepanel.fastapi  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402


def make_app() -> FastAPI:
    app = FastAPI(debug=True)
    pages = Jinja2Templates(env=jinja2.Environment(loader=jinja2.DictLoader({"home.html": "<h1>{{ title }}</h1>"})))

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return pages.TemplateResponse(request, "home.html", {"title": "Store"})

    tracepanel.fastapi.install(app)
    return app


@pytest.fixture
def client(traces: list[Trace]) -> Generator[TestClient, None, None]:
    previous = conf._framework  # noqa: SLF001
    with TestClient(make_app()) as test_client:
        yield test_client
    conf.use_source(previous)


def test_a_jinja2templates_render_is_a_template_span_in_the_view(client: TestClient, traces: list[Trace]) -> None:
    response = client.get("/")
    (trace,) = traces
    (view,) = [span for span in trace.spans if span.kind == "view"]
    (template,) = [span for span in trace.spans if span.kind == "template"]

    assert "<h1>Store</h1>" in response.text
    assert (template.name, template.parent_id) == ("home.html", view.id)
