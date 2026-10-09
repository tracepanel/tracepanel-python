import pytest
from django.test import Client

from tracepanel.core.model import Trace
from tracepanel.integrations import htmx

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true", "HX-Target": "rows", "HX-Trigger": "save"}


def test_htmx_request_and_response_headers(client: Client, traces: list[Trace]) -> None:
    client.get("/htmx/row/", headers=HTMX)

    (trace,) = traces
    assert {key: value for key, value in trace.attrs.items() if key.startswith("htmx.")} == {
        "htmx.request": True,
        "htmx.target": "rows",
        "htmx.trigger": "save",
        "htmx.response.trigger": "rowSaved",
    }
    assert trace.issues == []


def test_redirect_then_whole_page(client: Client, traces: list[Trace]) -> None:
    # The browser follows the redirect inside the XHR, with htmx's headers: two traces, one issue each.
    client.get("/htmx/login-needed/", headers=HTMX, follow=True)

    redirected, page = traces
    assert [(issue.kind, issue.title, issue.hint) for issue in redirected.issues] == [
        ("htmx", "htmx request redirected · 302 to /html-page/", htmx.REDIRECT_HINT),
    ]

    assert [(issue.kind, issue.title, issue.hint) for issue in page.issues] == [
        ("htmx", "Whole page sent to htmx · GET /html-page/", htmx.FULL_PAGE_HINT),
    ]


def test_boosted_page_is_fine(client: Client, traces: list[Trace]) -> None:
    client.get("/html-page/", headers={**HTMX, "HX-Boosted": "true"})

    (trace,) = traces
    assert (trace.attrs["htmx.boosted"], trace.issues) == (True, [])


def test_plain_requests_have_no_htmx_attrs(client: Client, traces: list[Trace]) -> None:
    client.get("/html-page/")

    (trace,) = traces
    assert [key for key in trace.attrs if key.startswith("htmx.")] == []
