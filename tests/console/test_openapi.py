"""Every API response matches the console's contract (tracepanel-agent's schema/openapi.yaml), through the app."""

import importlib.util
import json
from collections.abc import Callable
from urllib.parse import quote

import pytest
import yaml
from dirty_equals import IsPositiveInt
from django.contrib.auth.models import User
from django.test import Client
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from tests.agentstore import AGENT_SCHEMAS, AgentStore
from tracepanel.console import app
from tracepanel.storage.transport import set_writer

BASE = "https://tracepanel.dev/schema/0.1/"
OPENAPI = yaml.safe_load((AGENT_SCHEMAS / "openapi.yaml").read_text())
TRACE = json.loads((AGENT_SCHEMAS / "trace.schema.json").read_text())
REGISTRY = Registry().with_resources(
    [
        (f"{BASE}openapi.yaml", DRAFT202012.create_resource(OPENAPI)),
        (f"{BASE}trace.schema.json", DRAFT202012.create_resource(TRACE)),
    ]
)

Data = dict[str, str]


def response_schema(template: str, status: int, method: str = "get") -> Draft202012Validator:
    """Validator for one documented response, resolved inside the OpenAPI document."""
    response = OPENAPI["paths"][template][method]["responses"][str(status)]
    pointer = "/paths/" + template.replace("~", "~0").replace("/", "~1") + f"/{method}/responses/{status}"
    if "$ref" in response:
        pointer = response["$ref"].removeprefix("#")
    schema = {"$ref": f"{BASE}openapi.yaml#{quote(pointer)}/content/application~1json/schema"}
    return Draft202012Validator(schema, registry=REGISTRY)


@pytest.fixture
def data(client: Client, users: list[User], store: AgentStore) -> Data:
    """Requests of every kind in the store, and ids to ask the API about."""
    store.max_traces = 100
    writer = store.writer()
    set_writer(writer)
    client.raise_request_exception = False
    try:
        for path in ("/n-plus-one/", "/duplicates/", "/logs/", "/error/", "/welcome-email/", "/create-group/"):
            client.get(path)
        # Strawberry's view, or Graphene's in the environment that has Graphene instead.
        graphql = "/graphql/" if importlib.util.find_spec("strawberry") else "/graphene/"
        client.post(graphql, {"query": "query Team { users { username } }"}, content_type="application/json")
        writer.flush()
    finally:
        set_writer(None)
    select = next(query for query in store.list_calls("sql") if str(query["template"]).startswith("SELECT"))
    insert = next(query for query in store.list_calls("sql") if str(query["template"]).startswith("INSERT"))
    (issue,) = store.list_issues("kind:error")
    return {
        "trace": str(select["sample_trace"]),
        "select": str(select["sample_span"]),
        "insert_trace": str(insert["sample_trace"]),
        "insert": str(insert["sample_span"]),
        "issue": str(issue["fingerprint"]),
    }


CASES: list[tuple[str, int, Callable[[Data], str]]] = [
    ("/traces", 200, lambda _: "traces"),
    ("/traces", 200, lambda d: f"traces?ids={d['trace']}"),
    ("/traces", 400, lambda _: "traces?q=colour:red"),
    ("/traces/{id}", 200, lambda d: f"traces/{d['trace']}"),
    ("/traces/{id}", 404, lambda _: "traces/" + "0" * 26),
    # Not stored (yet), to a client that waits for it: an agent after 0.9.1 answers 204.
    *(
        [("/traces/{id}", 204, lambda _: "traces/" + "0" * 26 + "?missing=no-content")]
        if "204" in OPENAPI["paths"]["/traces/{id}"]["get"]["responses"]
        else []
    ),
    ("/traces/overview", 200, lambda _: "traces/overview"),
    ("/traces/overview", 200, lambda _: "traces/overview?q=route:/nope/"),
    ("/traces/overview", 400, lambda _: "traces/overview?q=since:yesterday"),
    ("/logs", 200, lambda _: "logs"),
    ("/logs", 400, lambda _: "logs?q=level:loud"),
    ("/mails", 200, lambda _: "mails"),
    ("/mails", 400, lambda _: "mails?q=subject:hi"),
    ("/calls", 200, lambda _: "calls"),
    ("/calls", 200, lambda _: "calls?system=sql"),
    ("/calls", 400, lambda _: "calls?q=colour:red"),
    ("/calls/summary", 200, lambda _: "calls/summary?system=sql"),
    ("/calls/summary", 400, lambda _: "calls/summary?q=colour:red"),
    ("/explain", 200, lambda d: f"explain?trace={d['trace']}&span={d['select']}"),
    ("/explain", 422, lambda d: f"explain?trace={d['insert_trace']}&span={d['insert']}"),
    ("/explain", 404, lambda d: f"explain?trace={d['trace']}&span=nope"),
    ("/endpoints", 200, lambda _: "endpoints"),
    ("/endpoints", 400, lambda _: "endpoints?q=status>=500"),
    ("/endpoints/detail", 200, lambda _: "endpoints/detail?method=GET&route=/n-plus-one/"),
    ("/endpoints/detail", 404, lambda _: "endpoints/detail?method=GET&route=/nope/"),
    ("/issues", 200, lambda _: "issues"),
    ("/issues", 400, lambda _: "issues?q=since:never"),
    ("/issues/detail", 200, lambda d: f"issues/detail?fingerprint={d['issue']}"),
    ("/issues/detail", 404, lambda _: "issues/detail?fingerprint=0000000000000000"),
    ("/issues/detail", 400, lambda d: f"issues/detail?fingerprint={d['issue']}&q=colour:red"),
    ("/counts", 200, lambda _: "counts"),
    # What the app adds to the agent's console: EXPLAIN, switching users.
    ("/capabilities", 200, lambda _: "capabilities"),
    ("/values", 200, lambda _: "values"),
    ("/search", 200, lambda _: "search"),
    ("/tasks", 200, lambda _: "tasks"),
    ("/tasks", 400, lambda _: "tasks?q=colour:red"),
    ("/services", 200, lambda _: "services"),
    ("/websockets", 200, lambda _: "websockets"),
    ("/graphql", 200, lambda _: "graphql"),
    ("/graphql", 400, lambda _: "graphql?q=colour:red"),
    ("/graphql/detail", 200, lambda _: "graphql/detail?operation=query Team"),
    ("/traces/summary", 200, lambda _: "traces/summary?q=kind:http"),
    ("/traces/summary", 400, lambda _: "traces/summary?q=colour:red"),
    ("/traces/summary", 404, lambda _: "traces/summary?q=kind:websocket"),
    ("/jobs", 200, lambda _: "jobs"),
    ("/jobs", 400, lambda _: "jobs?q=colour:red"),
    ("/rpc", 200, lambda _: "rpc"),
    ("/rpc", 400, lambda _: "rpc?q=colour:red"),
    ("/pages", 200, lambda _: "pages"),
    ("/pages", 400, lambda _: "pages?q=colour:red"),
    ("/snapshot/{id}", 200, lambda d: f"snapshot/{d['trace']}"),
    ("/snapshot/{id}", 404, lambda _: "snapshot/" + "0" * 26),
    # The agent's license and notices, as plain text.
    ("/licenses", 200, lambda _: "licenses"),
    ("/graphql/detail", 404, lambda _: "graphql/detail?operation=query Nope"),
    ("/websockets", 400, lambda _: "websockets?q=colour:red"),
    ("/services", 400, lambda _: "services?q=colour:red"),
    ("/traces/{id}/related", 200, lambda d: f"traces/{d['trace']}/related"),
    ("/users", 200, lambda _: "users?q=user"),
    ("/users", 403, lambda _: "users?remote=1"),
    ("/users/me", 200, lambda _: "users/me"),
    ("/users/me", 403, lambda _: "users/me?remote=1"),
]
# Newer than the agent CI runs (the released one): checked where the agent next to this checkout documents them.
NEWER: list[tuple[str, int, Callable[[Data], str]]] = [
    ("/logs/series", 200, lambda _: "logs/series"),
    ("/logs/series", 400, lambda _: "logs/series?q=level:loud"),
    ("/tasks/live", 200, lambda _: "tasks/live"),
    # What the app said of itself in its hello: settings and versions.
    ("/app", 200, lambda _: "app?service=shop"),
    ("/aws", 200, lambda _: "aws"),
    ("/aws", 400, lambda _: "aws?q=colour:red"),
    ("/aws/calls", 200, lambda _: "aws/calls?q=aws:S3"),
    ("/aws/calls", 400, lambda _: "aws/calls?q=colour:red"),
    # A trace and the chain of its calls: the traces, and their logs, calls and issues together.
    ("/traces/{id}/chain", 200, lambda d: f"traces/{d['trace']}/chain"),
    ("/traces/{id}/chain", 404, lambda _: "traces/" + "0" * 26 + "/chain"),
    ("/traces/{id}/logs", 200, lambda d: f"traces/{d['trace']}/logs"),
    ("/traces/{id}/logs", 404, lambda _: "traces/" + "0" * 26 + "/logs"),
    ("/traces/{id}/calls", 200, lambda d: f"traces/{d['trace']}/calls"),
    ("/traces/{id}/calls", 404, lambda _: "traces/" + "0" * 26 + "/calls"),
    ("/traces/{id}/issues", 200, lambda d: f"traces/{d['trace']}/issues"),
    ("/traces/{id}/issues", 404, lambda _: "traces/" + "0" * 26 + "/issues"),
]
CASES += [case for case in NEWER if case[0] in OPENAPI["paths"]]


# Every name the agent's contract uses: a capability it doesn't know is not in it.
CONTRACT = json.dumps(OPENAPI)


def documented(template: str, body: object) -> object:
    """The answer, less a capability the agent echoes from an app's hello but doesn't document yet.

    The agent offers what a hello names (this run's Celery tests say `live_tasks`), and one before `live_tasks` doesn't
    document it: CI runs one. A local run's sibling agent documents it, and is held to it.
    """
    if template != "/capabilities" or not isinstance(body, dict):
        return body
    return {key: value for key, value in body.items() if f'"{key}"' in CONTRACT}


@pytest.mark.django_db
@pytest.mark.parametrize(("template", "status", "url"), CASES)
def test_responses_match_the_contract(
    client: Client, data: Data, template: str, status: int, url: Callable[[Data], str]
) -> None:
    path = url(data)
    # "?remote=1" marks a request from another machine: user switching refuses those.
    remote = "10.0.0.5" if "remote=1" in path else "127.0.0.1"
    response = client.get(f"/__tracepanel__/api/{path}", REMOTE_ADDR=remote)

    assert response.status_code == status

    if status == 204:
        # No Content: no body.
        assert response.content == b""
    elif "text/plain" in OPENAPI["paths"][template]["get"]["responses"][str(status)].get("content", {}):
        assert response["Content-Type"].startswith("text/plain")
        assert response.content
    else:
        response_schema(template, status).validate(documented(template, response.json()))


def test_what_the_app_serves_itself_is_in_the_contract() -> None:
    # EXPLAIN and switching users; the agent serves the rest.
    assert {*app.WRITES, *app.EXTENSIONS} <= {path.lstrip("/") for path in OPENAPI["paths"]}


def test_every_documented_response_is_exercised() -> None:
    # GET endpoints; the one write (/users/switch) has its own test.
    documented = {
        (path, int(status))
        for path, item in OPENAPI["paths"].items()
        if "get" in item
        for status in item["get"]["responses"]
    }

    assert documented == {(template, status) for template, status, _ in CASES}


@pytest.mark.django_db
def test_clearing_the_store(client: Client, data: Data, store: AgentStore) -> None:
    # Only DELETE: another site can't send one without a CORS preflight, and the console never allows one.
    assert client.get("/__tracepanel__/api/store").status_code == 405
    assert client.post("/__tracepanel__/api/store").status_code == 405
    assert store.counts()["issues"] == IsPositiveInt

    response = client.delete("/__tracepanel__/api/store")

    assert response.status_code == 200
    response_schema("/store", 200, "delete").validate(response.json())
    assert response.json() == {"deleted": IsPositiveInt}

    assert store.search_traces("") == ([], None)
    assert store.search_logs("") == ([], None)
    assert store.list_mails() == ([], None)
    assert store.list_calls() == []
    assert store.list_issues() == []
