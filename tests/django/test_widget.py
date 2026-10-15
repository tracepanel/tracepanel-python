import pytest
from dirty_equals import IsStr
from django.contrib.auth.models import User
from django.test import Client
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore
from tracepanel.core.model import Trace

pytestmark = pytest.mark.django_db


def test_headers(client: Client, traces: list[Trace], users: list[User]) -> None:
    response = client.get("/n-plus-one/")
    trace = traces[0]
    assert response["Tracepanel-Id"] == trace.id
    assert response["Tracepanel-Url"] == f"http://testserver/__tracepanel__/#/t/{trace.id}"
    timing = response["Server-Timing"]
    assert timing.startswith("db;dur=")
    # The agent finds the issues once the trace is stored: the widget asks the console.
    assert 'tracepanel-issues;desc="pending"' in timing
    # Named after the route: the widget shows the request line itself.
    assert "Tracepanel-Name" not in response


def test_script_injected_into_html(client: Client, traces: list[Trace], db: None, store: AgentStore) -> None:
    body = client.get("/html/").content.decode()
    assert '<script src="/__tracepanel__/widget.min.js"' in body
    assert f'data-tracepanel-id="{traces[0].id}"' in body
    # What served the page, for the panel's runtime chip.
    assert 'data-tracepanel-runtime="Python ' in body
    assert body.index("<script") < body.index("</body>")


def test_the_page_says_when_the_user_can_be_switched(
    client: Client, traces: list[Trace], db: None, store: AgentStore, settings: Settings
) -> None:
    settings.DEBUG = True
    switchable = client.get("/html/").content.decode()
    settings.TRACEPANEL = {"user_switch": False}
    fixed = client.get("/html/").content.decode()

    assert 'data-tracepanel-users="1"' in switchable
    assert "data-tracepanel-users" not in fixed


def test_script_runs_before_the_page_scripts(client: Client, traces: list[Trace], db: None, store: AgentStore) -> None:
    # GraphiQL and Swagger keep a reference to fetch while loading: the widget must wrap it first.
    body = client.get("/html-page/").content.decode()

    assert body.index('<script src="/__tracepanel__/widget.min.js"') < body.index('<script src="/app.js">')
    assert body.index("<head>") < body.index("widget.min.js") < body.index("<title>")
    assert " defer" not in body[body.index("widget.min.js") : body.index("</script>")]


def test_json_is_not_touched(client: Client, traces: list[Trace], db: None) -> None:
    assert b"<script" not in client.get("/duplicates/").content


def test_widget_can_be_disabled(client: Client, traces: list[Trace], db: None, settings: Settings) -> None:
    settings.TRACEPANEL = {"widget": False}
    assert b"<script" not in client.get("/html/").content


def test_without_an_agent_no_script_and_the_console_says_why(client: Client, traces: list[Trace], db: None) -> None:
    # The panel is the agent's: without one (the tests turn it off) the page gets the headers, not the script.
    body = client.get("/html/").content.decode()
    console = client.get("/__tracepanel__/")
    api = client.get("/__tracepanel__/api/counts")

    assert "<script" not in body

    assert (console.status_code, "the agent is off" in console.content.decode()) == (503, True)
    assert (api.status_code, api.json()) == (503, {"error": IsStr(regex="No tracepanel agent: the agent is off.*")})
