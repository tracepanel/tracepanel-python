import pytest
from dirty_equals import IsStr
from django.test import Client

from tests.agentstore import issues
from tracepanel.core.model import Trace

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true", "HX-Target": "rows", "HX-Trigger": "save"}


def test_a_request_from_another_app_continues_its_trace(client: Client, traces: list[Trace]) -> None:
    client.get("/htmx/row/", headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})

    (trace,) = traces
    assert (trace.attrs["w3c.trace_id"], trace.attrs["w3c.parent_span_id"]) == (
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "00f067aa0ba902b7",
    )
    # A request with none starts its own.
    client.get("/htmx/row/")
    assert "w3c.parent_span_id" not in traces[1].attrs


def test_htmx_request_and_response_headers(client: Client, traces: list[Trace]) -> None:
    client.get("/htmx/row/", headers=HTMX)

    (trace,) = traces
    assert {key: value for key, value in trace.attrs.items() if key.startswith("htmx.")} == {
        "htmx.request": True,
        "htmx.target": "rows",
        "htmx.trigger": "save",
        "htmx.response.trigger": "rowSaved",
    }
    assert issues(trace) == []


def test_redirect_then_whole_page(client: Client, traces: list[Trace]) -> None:
    # The browser follows the redirect inside the XHR, with htmx's headers: two traces, one issue each.
    client.get("/htmx/login-needed/", headers=HTMX, follow=True)

    redirected, page = traces
    assert [(issue.kind, issue.title, issue.hint) for issue in issues(redirected)] == [
        ("htmx", "htmx request redirected · 302 to /html-page/", IsStr(regex=".*HX-Redirect.*")),
    ]

    assert [(issue.kind, issue.title, issue.hint) for issue in issues(page)] == [
        ("htmx", "Whole page sent to htmx · GET /html-page/", IsStr(regex=".*fragment.*")),
    ]


def test_boosted_page_is_fine(client: Client, traces: list[Trace]) -> None:
    client.get("/html-page/", headers={**HTMX, "HX-Boosted": "true"})

    (trace,) = traces
    assert (trace.attrs["htmx.boosted"], issues(trace)) == (True, [])


def test_plain_requests_have_no_htmx_attrs(client: Client, traces: list[Trace]) -> None:
    client.get("/html-page/")

    (trace,) = traces
    assert [key for key in trace.attrs if key.startswith("htmx.")] == []
