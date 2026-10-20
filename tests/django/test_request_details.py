import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from tracepanel.core import conf
from tracepanel.core.model import Trace

pytestmark = pytest.mark.django_db


def details(trace: Trace) -> dict[str, object]:
    """The attributes of what the request carried."""
    prefixes = ("http.request.header.", "http.response.header.", "http.request.cookie.", "session.")
    keys = ("url.query", "http.request.body", "http.request.body.truncated")
    return {key: value for key, value in trace.attrs.items() if key.startswith(prefixes) or key in keys}


def test_a_request_records_its_headers_query_and_cookies_with_secrets_masked(
    client: Client, traces: list[Trace]
) -> None:
    client.cookies["theme"] = "dark"
    client.cookies["sessionid"] = "s-1"

    client.get("/?page=2&token=abc", headers={"X-Request-Id": "r-1", "Authorization": "Bearer abc"})

    (trace,) = traces
    assert details(trace) == {
        "http.request.header.x_request_id": "r-1",
        "http.request.header.authorization": "********",
        "url.query": "page=2&token=********",
        "http.request.cookie.theme": "dark",
        "http.request.cookie.sessionid": "********",
        "http.response.header.content_type": "application/json",
        "http.response.header.vary": "Cookie",
    }


def test_a_body_the_view_read_is_recorded_masked(client: Client, traces: list[Trace]) -> None:
    client.post("/echo-json/", '{"email": "ann@example.com", "password": "p"}', content_type="application/json")

    (trace,) = traces
    assert trace.attrs["http.request.body"] == '{"email":"ann@example.com","password":"********"}'


def test_a_form_with_a_file_is_recorded_by_its_fields_and_the_files_sizes(client: Client, traces: list[Trace]) -> None:
    report = SimpleUploadedFile("report.pdf", b"%PDF-1.7 0123456789", content_type="application/pdf")

    client.post("/upload/", {"title": "Q3", "password": "p", "file": report})

    (trace,) = traces
    assert trace.attrs["http.request.body"] == "title=Q3&password=********&file=<file report.pdf, 19 bytes>"


def test_a_body_the_view_never_read_is_not_read_for_it(client: Client, traces: list[Trace]) -> None:
    client.post("/", '{"password": "p"}', content_type="application/json")

    (trace,) = traces
    assert "http.request.body" not in trace.attrs


def test_the_session_the_view_used_is_recorded_masked(client: Client, traces: list[Trace]) -> None:
    client.get("/remember/")

    (trace,) = traces
    assert {key: value for key, value in details(trace).items() if key.startswith("session.")} == {
        "session.cart": "[1, 2]",
        "session.oauth_token": "********",
    }


def test_nothing_is_recorded_with_capture_request_off(client: Client, traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_request": False}
    conf.reset()
    client.cookies["theme"] = "dark"

    client.post("/echo-json/?page=2", '{"a": 1}', content_type="application/json", headers={"X-Request-Id": "r-1"})

    (trace,) = traces
    assert details(trace) == {}
