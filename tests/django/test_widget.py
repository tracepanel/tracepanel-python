import pytest
from django.contrib.auth.models import User
from django.test import Client
from pytest_django.fixtures import Settings

from tracepanel.core.model import Trace
from tracepanel.storage import transport

pytestmark = pytest.mark.django_db


def test_headers(client: Client, traces: list[Trace], users: list[User]) -> None:
    response = client.get("/n-plus-one/")
    trace = traces[0]
    assert response["Tracepanel-Id"] == trace.id
    assert response["Tracepanel-Url"] == f"http://testserver/__tracepanel__/#/t/{trace.id}"
    timing = response["Server-Timing"]
    assert timing.startswith("db;dur=")
    assert f'tracepanel-issues;desc="{len(trace.issues)}"' in timing
    # Named after the route: the widget shows the request line itself.
    assert "Tracepanel-Name" not in response


def test_script_injected_into_html(client: Client, traces: list[Trace], db: None) -> None:
    body = client.get("/html/").content.decode()
    assert '<script src="/__tracepanel__/widget.js"' in body
    assert f'data-tracepanel-id="{traces[0].id}"' in body
    # What served the page, for the panel's runtime chip.
    assert 'data-tracepanel-runtime="Python ' in body
    assert body.index("<script") < body.index("</body>")


def test_script_runs_before_the_page_scripts(client: Client, traces: list[Trace], db: None) -> None:
    # GraphiQL and Swagger keep a reference to fetch while loading: the widget must wrap it first.
    body = client.get("/html-page/").content.decode()

    assert body.index('<script src="/__tracepanel__/widget.js"') < body.index('<script src="/app.js">')
    assert body.index("<head>") < body.index("widget.js") < body.index("<title>")
    assert " defer" not in body[body.index("widget.js") : body.index("</script>")]


def test_json_is_not_touched(client: Client, traces: list[Trace], db: None) -> None:
    assert b"<script" not in client.get("/duplicates/").content


def test_widget_can_be_disabled(client: Client, traces: list[Trace], db: None, settings: Settings) -> None:
    settings.TRACEPANEL = {"widget": False}
    assert b"<script" not in client.get("/html/").content


def test_issues_the_agent_finds_are_pending(
    client: Client, traces: list[Trace], users: list[User], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Where traces go their issues are found later: the app doesn't look for them, the widget asks the console.
    monkeypatch.setattr(type(transport.get_writer()), "analyzes_later", True, raising=False)

    response = client.get("/n-plus-one/")
    body = client.get("/html/").content.decode()

    assert 'tracepanel-issues;desc="pending"' in response["Server-Timing"]
    assert traces[0].issues == []

    assert 'data-tracepanel-issues="pending"' in body
