import asyncio
import sqlite3

import pytest

jinja2 = pytest.importorskip("jinja2")

from tracepanel.core import conf, values  # noqa: E402 (optional test group)
from tracepanel.core.context import start_trace  # noqa: E402
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


def test_a_render_keeps_the_context_it_got_masked() -> None:
    environment = jinja2.Environment(loader=LOADER, autoescape=True)

    with start_trace("http", "GET /") as trace:
        environment.get_template("page.html").render({"title": "Hi"}, token="t-1", dsn="https://key@sentry.io/1")

    (span,) = [span for span in trace.spans if span.kind == "template"]

    assert span.attrs == {
        "template.name": "page.html",
        "template.context.names": ["title", "token", "dsn"],
        "template.context.values": ["'Hi'", "********", "'https://********@sentry.io/1'"],
    }


def test_an_async_render_keeps_its_context_once() -> None:
    environment = jinja2.Environment(loader=LOADER, autoescape=True, enable_async=True)

    with start_trace("http", "GET /") as trace:
        environment.get_template("page.html").render(title="Hi")
        asyncio.run(environment.get_template("row.html").render_async(rows=[1, 2]))

    assert [span.attrs for span in trace.spans if span.kind == "template"] == [
        {
            "template.name": "page.html",
            "template.context.names": ["title"],
            "template.context.values": ["'Hi'"],
        },
        {
            "template.name": "row.html",
            "template.context.names": ["rows"],
            "template.context.values": ["[1, 2]"],
        },
    ]


def test_the_context_is_left_out_when_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_CAPTURE_TEMPLATE_CONTEXT", "false")
    conf.reset()

    with start_trace("http", "GET /") as trace:
        jinja2.Environment(autoescape=True).from_string("{{ a }}").render(a=1)

    assert [span.attrs for span in trace.spans if span.kind == "template"] == [{"template.name": "<string template>"}]


def test_a_trace_keeps_the_contexts_of_each_templates_first_renders_only() -> None:
    environment = jinja2.Environment(loader=jinja2.DictLoader({"row.html": "{{ n }}"}), autoescape=True)

    with start_trace("http", "GET /") as trace:
        for n in range(5):
            environment.get_template("row.html").render(n=n)

    spans = [span for span in trace.spans if span.kind == "template"]

    assert [span.attrs for span in spans] == [
        *[
            {"template.name": "row.html", "template.context.names": ["n"], "template.context.values": [str(n)]}
            for n in range(values.MAX_CONTEXTS_PER_TEMPLATE)
        ],
        *[{"template.name": "row.html"}] * (5 - values.MAX_CONTEXTS_PER_TEMPLATE),
    ]


def test_a_trace_keeps_so_many_contexts_in_all() -> None:
    names = [f"t{n}.html" for n in range(values.MAX_CONTEXTS + 5)]
    environment = jinja2.Environment(loader=jinja2.DictLoader(dict.fromkeys(names, "x")), autoescape=True)

    with start_trace("http", "GET /") as trace:
        for name in names:
            environment.get_template(name).render(n=1)

    kept = [span for span in trace.spans if span.kind == "template" and "template.context.names" in span.attrs]

    assert [span.name for span in kept] == names[: values.MAX_CONTEXTS]
