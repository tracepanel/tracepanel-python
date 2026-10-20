import asyncio
import sqlite3

import pytest

jinja2 = pytest.importorskip("jinja2")

from tracepanel.core.context import start_trace  # noqa: E402 (optional test group)
from tracepanel.core.model import Trace  # noqa: E402
from tracepanel.integrations import dbapi  # noqa: E402
from tracepanel.integrations import jinja as integration  # noqa: E402

integration.install()
LOADER = jinja2.DictLoader({"page.html": "<h1>{{ title }}</h1>{% include 'row.html' %}", "row.html": "<p>row</p>"})


def templates(trace: Trace) -> list[tuple[str, object]]:
    return [(span.name, span.attrs["template.name"]) for span in trace.spans if span.kind == "template"]


def test_each_render_is_a_template_span_named_after_its_file() -> None:
    environment = jinja2.Environment(loader=LOADER, autoescape=True)

    with start_trace("http", "GET /") as trace:
        html = environment.get_template("page.html").render(title="Hi")

    assert html == "<h1>Hi</h1><p>row</p>"
    # The include renders inside the page's span.
    assert templates(trace) == [("page.html", "page.html")]


def test_a_template_from_a_string_is_named_so() -> None:
    with start_trace("http", "GET /") as trace:
        jinja2.Environment(autoescape=True).from_string("{{ 1 + 1 }}").render()

    assert templates(trace) == [("<string template>", "<string template>")]


def test_under_an_async_environment_a_render_is_one_span() -> None:
    environment = jinja2.Environment(loader=LOADER, autoescape=True, enable_async=True)

    with start_trace("http", "GET /") as trace:
        environment.get_template("page.html").render(title="Hi")
        asyncio.run(environment.get_template("row.html").render_async())

    assert templates(trace) == [("page.html", "page.html"), ("row.html", "row.html")]


def test_a_query_run_while_rendering_names_its_template() -> None:
    dbapi.install()
    connection = sqlite3.connect(":memory:")
    environment = jinja2.Environment(autoescape=True)
    environment.globals["count"] = lambda: connection.execute("SELECT 1").fetchone()[0]

    with start_trace("http", "GET /") as trace:
        environment.from_string("{{ count() }}").render()

    (query,) = [span for span in trace.spans if span.kind == "sql"]
    (template,) = [span for span in trace.spans if span.kind == "template"]
    assert (query.parent_id, query.attrs["code.template"]) == (template.id, "<string template>")
