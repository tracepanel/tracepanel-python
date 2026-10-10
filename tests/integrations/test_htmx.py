import pytest
from dirty_equals import IsStr

from tests.agentstore import issues
from tracepanel.core.context import start_trace
from tracepanel.core.model import Attrs
from tracepanel.integrations import htmx

HTMX = {"hx-request": "true", "hx-target": "rows", "hx-trigger": "reload", "hx-current-url": "http://site/p/"}


def test_request_attrs() -> None:
    assert htmx.request_attrs(HTMX) == {
        "htmx.request": True,
        "htmx.trigger": "reload",
        "htmx.target": "rows",
        "htmx.current_url": "http://site/p/",
    }

    assert htmx.request_attrs({"hx-request": "true", "hx-boosted": "true"}) == {
        "htmx.request": True,
        "htmx.boosted": True,
    }

    assert htmx.request_attrs({"accept": "text/html"}) == {}


@pytest.mark.parametrize(
    ("request_attrs", "status", "headers", "body", "expected"),
    [
        pytest.param(
            {"htmx.request": True},
            200,
            {"content-type": "text/html", "hx-trigger": "saved", "hx-reswap": "outerHTML"},
            b"<tr><td>row</td></tr>",
            {"htmx.response.trigger": "saved", "htmx.response.reswap": "outerHTML"},
            id="fragment",
        ),
        pytest.param(
            {"htmx.request": True},
            302,
            {"location": "/login/?next=/p/"},
            b"",
            {"htmx.redirected_to": "/login/?next=/p/"},
            id="redirect",
        ),
        pytest.param(
            {"htmx.request": True},
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"\n  <!DOCTYPE html><html><body>login</body></html>",
            {"htmx.full_page": True},
            id="full page",
        ),
        pytest.param(
            {"htmx.request": True, "htmx.boosted": True},
            200,
            {"content-type": "text/html"},
            b"<!doctype html><html></html>",
            {},
            id="boosted asks for a page",
        ),
        pytest.param(
            {"htmx.request": True},
            500,
            {"content-type": "text/html"},
            b"<!DOCTYPE html><html><body>Traceback</body></html>",
            {},
            id="error page is not swapped",
        ),
        pytest.param({}, 302, {"location": "/login/"}, b"", {}, id="not htmx"),
    ],
)
def test_response_attrs(
    request_attrs: Attrs, status: int, headers: dict[str, str], body: bytes, expected: Attrs
) -> None:
    assert htmx.response_attrs(request_attrs, status, headers, body) == expected


def test_redirect_and_full_page_are_issues() -> None:
    with start_trace("http", "GET /p/", {"htmx.request": True, "http.method": "GET", "http.route": "/p/"}) as trace:
        trace.attrs.update({"http.status_code": 302, "htmx.redirected_to": "/login/", "htmx.full_page": True})

    # What this module records, the agent turns into issues.
    assert [(issue.kind, issue.title, issue.hint) for issue in issues(trace)] == [
        ("htmx", "htmx request redirected · 302 to /login/", IsStr(regex=".*HX-Redirect.*")),
        ("htmx", "Whole page sent to htmx · GET /p/", IsStr(regex=".*fragment.*")),
    ]
