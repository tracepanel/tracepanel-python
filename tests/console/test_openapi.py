"""Every API response matches schema/openapi.yaml, the contract the console, the agent and the cloud share."""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from django.contrib.auth.models import User
from django.test import Client
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from tracepanel.console import app
from tracepanel.storage.store import Store
from tracepanel.storage.transport import StoreWriter, set_writer

SCHEMAS = Path(__file__).resolve().parents[2] / "schema"
BASE = "https://tracepanel.dev/schema/0.1/"
OPENAPI = yaml.safe_load((SCHEMAS / "openapi.yaml").read_text())
TRACE = json.loads((SCHEMAS / "trace.schema.json").read_text())
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
def data(client: Client, users: list[User], store: Store) -> Data:
    """Requests of every kind in the store, and ids to ask the API about."""
    store.max_traces = 100
    writer = StoreWriter(store)
    set_writer(writer)
    client.raise_request_exception = False
    try:
        for path in ("/n-plus-one/", "/duplicates/", "/logs/", "/error/", "/welcome-email/", "/create-group/"):
            client.get(path)
        if importlib.util.find_spec("strawberry"):
            client.post("/graphql/", {"query": "query Team { users { username } }"}, content_type="application/json")
        writer.flush()
    finally:
        set_writer(None)
    select = next(query for query in store.list_queries() if str(query["statement"]).startswith("SELECT"))
    insert = next(query for query in store.list_queries() if str(query["statement"]).startswith("INSERT"))
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
    ("/traces/overview", 200, lambda _: "traces/overview"),
    ("/traces/overview", 200, lambda _: "traces/overview?q=route:/nope/"),
    ("/traces/overview", 400, lambda _: "traces/overview?q=since:yesterday"),
    ("/logs", 200, lambda _: "logs"),
    ("/logs", 400, lambda _: "logs?q=level:loud"),
    ("/mails", 200, lambda _: "mails"),
    ("/mails", 400, lambda _: "mails?q=subject:hi"),
    ("/queries", 200, lambda _: "queries"),
    ("/queries", 400, lambda _: "queries?q=colour:red"),
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
    ("/counts", 200, lambda _: "counts"),
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
    ("/graphql/detail", 404, lambda _: "graphql/detail?operation=query Nope"),
    ("/websockets", 400, lambda _: "websockets?q=colour:red"),
    ("/services", 400, lambda _: "services?q=colour:red"),
    ("/traces/{id}/related", 200, lambda d: f"traces/{d['trace']}/related"),
    ("/users", 200, lambda _: "users?q=user"),
    ("/users", 403, lambda _: "users?remote=1"),
    ("/users/me", 200, lambda _: "users/me"),
    ("/users/me", 403, lambda _: "users/me?remote=1"),
]


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
    response_schema(template, status).validate(response.json())


def test_every_route_is_in_the_contract() -> None:
    assert {path.lstrip("/") for path in OPENAPI["paths"]} == {
        *app.RESOURCES,
        *app.WRITES,
        *app.EXTENSIONS,
        "traces/{id}",
        "traces/{id}/related",
    }


def test_every_documented_response_is_exercised() -> None:
    # GET endpoints; the one write (/users/switch) has its own test.
    documented = {
        (path, int(status))
        for path, item in OPENAPI["paths"].items()
        if "get" in item
        for status in item["get"]["responses"]
    }

    assert documented == {(template, status) for template, status, _ in CASES}
