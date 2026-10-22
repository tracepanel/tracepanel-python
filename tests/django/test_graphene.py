import pytest

pytest.importorskip("graphene_django")

import os
import subprocess
import sys

from dirty_equals import IsInt, IsList, IsPartialDict, IsStr
from django.contrib.auth.models import Group, User
from django.test import Client
from jsonschema import Draft202012Validator

from tests.agentstore import AgentStore, issues
from tracepanel.core.model import Trace
from tracepanel.storage.transport import set_writer


def post(
    client: Client, query: str, operation: str | None = None, variables: dict[str, object] | None = None
) -> dict[str, object]:
    body = {
        "query": query,
        **({"operationName": operation} if operation else {}),
        **({"variables": variables} if variables else {}),
    }
    return client.post("/graphene/", body, content_type="application/json").json()


def graphql_spans(trace: Trace) -> list[tuple[str, str]]:
    return [(span.kind, span.name) for span in trace.spans if span.kind == "graphql"]


@pytest.mark.django_db
def test_operation_names_the_request(
    client: Client, traces: list[Trace], users: list[User], validator: Draft202012Validator
) -> None:
    result = post(client, "query Team { users { username groups } }", "Team")
    (trace,) = traces
    resolvers = [span for span in trace.spans if span.kind == "function"]

    assert result == {"data": {"users": [{"username": f"user{i}", "groups": ["staff"]} for i in range(5)]}}
    assert trace.name == "GraphQL query Team"
    assert trace.attrs == IsPartialDict(
        {
            "http.route": "/graphene/",
            "graphql.operation": "query Team",
            "graphql.operation.type": "query",
            "graphql.operation.name": "Team",
            "graphql.document": "query Team { users { username groups } }",
            "graphql.errors": 0,
        }
    )

    assert graphql_spans(trace) == [("graphql", "query Team"), ("graphql", "parse"), ("graphql", "validate")]
    # Only the project's resolvers: Query.users once, UserType.groups per user; username is an attribute.
    assert [span.name for span in resolvers] == ["Query.users", *["UserType.groups"] * 5]
    assert resolvers[1].attrs == {
        "graphql.field": "UserType.groups",
        "graphql.path": "users.0.groups",
        "code.function": "UserType.groups",
    }

    validator.validate(trace.to_dict())


@pytest.mark.django_db
def test_operation_name_comes_from_the_document(client: Client, traces: list[Trace], users: list[User]) -> None:
    post(client, "query Team { users { id } }")
    (trace,) = traces

    # A model's id is an attribute too: no span for it.
    assert (trace.name, trace.attrs["graphql.operation.name"]) == ("GraphQL query Team", "Team")
    assert [span.name for span in trace.spans if span.kind == "function"] == ["Query.users"]


@pytest.mark.django_db
def test_operation_picked_by_name(client: Client, traces: list[Trace], users: list[User]) -> None:
    post(client, "query Team { users { id } } query Report { broken }", "Team")
    (trace,) = traces

    assert (trace.name, trace.attrs["graphql.errors"]) == ("GraphQL query Team", 0)


@pytest.mark.django_db
def test_mutation(client: Client, traces: list[Trace]) -> None:
    query = "mutation Add($name: String!) { createGroup(name: $name) { name } }"
    result = post(client, query, variables={"name": "editors"})
    (trace,) = traces

    assert result == {"data": {"createGroup": {"name": "editors"}}}
    assert Group.objects.filter(name="editors").exists()

    assert trace.name == "GraphQL mutation Add"
    assert trace.attrs == IsPartialDict(
        {
            "graphql.operation": "mutation Add",
            "graphql.operation.type": "mutation",
            "graphql.operation.name": "Add",
            "graphql.document": query,
            "graphql.errors": 0,
        }
    )
    (resolver,) = [span for span in trace.spans if "graphql.field" in span.attrs]
    assert (resolver.name, resolver.attrs["graphql.path"]) == ("Mutation.createGroup", "createGroup")
    # The INSERT is the resolver's.
    assert [span.name for span in trace.spans if span.parent_id == resolver.id and span.kind == "sql"] == [
        "INSERT auth_group"
    ]


@pytest.mark.django_db
def test_n_plus_one_in_a_resolver_suggests_a_dataloader(client: Client, traces: list[Trace], users: list[User]) -> None:
    post(client, "{ users { groups } }")
    (trace,) = traces
    (issue,) = issues(trace)

    assert (trace.name, issue.kind, issue.culprit) == (
        "GraphQL query { users }",
        "n+1",
        IsStr(regex=r"tests/project/graphene_schema\.py:\d+ in resolve_groups"),
    )
    assert "DataLoader" in (issue.hint or "")


@pytest.mark.django_db
def test_resolver_error_comes_back_as_200(client: Client, traces: list[Trace]) -> None:
    result = post(client, "query Report { broken }")
    (trace,) = traces

    assert result == IsPartialDict({"data": None, "errors": [IsPartialDict(message="the reporting service is down")]})
    assert (trace.status, trace.attrs["http.status_code"], trace.attrs["graphql.errors"]) == ("error", 200, 1)
    assert [(error.type, error.handled) for error in trace.errors] == [("builtins.RuntimeError", False)]
    assert [issue.kind for issue in issues(trace)] == ["error"]


@pytest.mark.django_db
def test_invalid_document_is_not_an_exception(client: Client, traces: list[Trace]) -> None:
    post(client, "query Typo { usres { id } }")
    post(client, "query {")
    invalid, unparsed = traces

    # The client's mistake, a 400: counted, and no error of the app's.
    assert (invalid.name, invalid.attrs["http.status_code"], invalid.attrs["graphql.errors"]) == (
        "GraphQL query Typo",
        400,
        1,
    )
    assert graphql_spans(invalid) == [("graphql", "query Typo"), ("graphql", "parse"), ("graphql", "validate")]
    assert invalid.errors == []

    # No operation at all: not a query, named apart from the anonymous ones.
    assert (unparsed.name, unparsed.attrs["graphql.errors"], unparsed.errors) == ("GraphQL syntax error", 1, [])
    assert unparsed.attrs == IsPartialDict(
        {
            "graphql.operation": "syntax error",
            "graphql.operation.type": None,
            "graphql.operation.name": None,
            "graphql.document": "query {",
        }
    )
    assert graphql_spans(unparsed) == [("graphql", "syntax error"), ("graphql", "parse")]


@pytest.mark.django_db
def test_syntax_error_is_not_an_anonymous_query(client: Client, traces: list[Trace], store: AgentStore) -> None:
    post(client, "{ ...Team } fragment Team on Query { users { id } }")
    post(client, "query Team {", "Team")
    store.write(traces)

    assert [trace.name for trace in traces] == ["GraphQL query anonymous", "GraphQL syntax error"]

    assert store.list_graphql() == IsList(
        IsPartialDict(operation="query anonymous", type="query", name=None, calls=1),
        IsPartialDict(operation="syntax error", type=None, name=None, calls=1),
        check_order=False,
    )


@pytest.mark.django_db
def test_batch_has_a_span_per_operation(client: Client, traces: list[Trace], users: list[User]) -> None:
    batch = [
        {"id": 1, "query": "query Team { users { username } }"},
        {"id": 2, "query": "query Report { broken }"},
    ]
    result = client.post("/graphene/batch/", batch, content_type="application/json").json()
    (trace,) = traces

    assert [(entry["id"], entry["status"]) for entry in result] == [(1, 200), (2, 200)]
    assert [name for kind, name in graphql_spans(trace) if name not in {"parse", "validate"}] == [
        "query Team",
        "query Report",
    ]
    assert [span.name for span in trace.spans if span.kind == "function"] == ["Query.users", "Query.broken"]
    # The request is named after the last operation; every operation's exceptions are its errors.
    assert trace.name == "GraphQL query Report"
    assert [error.type for error in trace.errors] == ["builtins.RuntimeError"]


@pytest.mark.django_db
def test_operations_page(client: Client, traces: list[Trace], store: AgentStore, users: list[User]) -> None:
    post(client, "query Team { users { username } }")
    post(client, "query Team { users { username } }")
    post(client, "query Report { broken }")
    store.write(traces)

    assert store.list_graphql() == IsList(
        {
            "operation": "query Team",
            "type": "query",
            "name": "Team",
            "calls": 2,
            "errors": 0,
            "p50": IsInt(ge=0),
            "p95": IsInt(ge=0),
            "total": IsInt(ge=0),
            "avg_queries": 1.0,
            "with_issues": 0,
            "last_seen": IsInt,
        },
        IsPartialDict(operation="query Report", calls=1, errors=1),
        check_order=False,
    )
    assert [row["operation"] for row in store.list_graphql('operation:"query Report"')] == ["query Report"]
    assert [trace["name"] for trace in store.search_traces('operation:"query Team"')[0]] == ["GraphQL query Team"] * 2


@pytest.mark.django_db
def test_operation_detail(client: Client, traces: list[Trace], store: AgentStore, users: list[User]) -> None:
    post(client, "query Team { users { username groups } }", "Team")
    post(client, "query Team { users { username groups } }", "Team")
    store.write(traces)
    detail = store.graphql_detail("query Team")

    assert detail == IsPartialDict(
        {
            "operation": "query Team",
            "type": "query",
            "name": "Team",
            "document": "query Team { users { username groups } }",
            "summary": IsPartialDict(calls=2),
        }
    )
    assert detail is not None
    assert detail["resolvers"] == IsList(
        {"field": "Query.users", "per_call": 1.0, "total": IsInt, "p95": IsInt, "queries_per_call": 1.0},
        {"field": "UserType.groups", "per_call": 5.0, "total": IsInt, "p95": IsInt, "queries_per_call": 5.0},
        check_order=False,
    )
    assert [item["graphql_operations"] for item in store.list_endpoints()] == [1]


@pytest.mark.django_db
def test_widget_gets_the_operation_name(client: Client, traces: list[Trace]) -> None:
    query = "query Team { users { username } }"
    response = client.post("/graphene/", {"query": query}, content_type="application/json")

    assert response["Tracepanel-Name"] == "GraphQL query Team"


@pytest.mark.django_db
def test_unnamed_operation_is_named_after_its_root_fields(client: Client, traces: list[Trace]) -> None:
    post(client, "{ users { username } groups: users { id } }")
    post(client, "mutation { a: __typename b: __typename c: __typename d: __typename }")

    assert [(trace.name, trace.attrs["graphql.operation.name"]) for trace in traces] == [
        ("GraphQL query { users, users }", None),
        ("GraphQL mutation { __typename, __typename, __typename, +1 }", None),
    ]


@pytest.mark.django_db
def test_http_overview_counts_graphql_as_one_endpoint(client: Client, store: AgentStore) -> None:
    writer = store.writer()
    set_writer(writer)
    try:
        post(client, "query Team { users { username } }")
        post(client, "query Groups { users { groups } }")
        writer.flush()
    finally:
        set_writer(None)

    by_endpoint = store.traces_overview("kind:http", group="endpoint")
    by_operation = store.traces_overview("kind:http")

    # The HTTP page sees one endpoint; the GraphQL page sees its operations.
    assert by_endpoint["slowest"] == [IsPartialDict(name="POST /graphene/", calls=2)]
    assert by_operation["slowest"] == IsList(
        IsPartialDict(name="GraphQL query Groups"), IsPartialDict(name="GraphQL query Team"), check_order=False
    )


@pytest.mark.parametrize(("enabled", "patched"), [("true", "True"), ("false", "False")])
def test_can_be_turned_off(enabled: str, patched: str) -> None:
    # Patches are applied once per process, at startup: a fresh interpreter shows what the setting does.
    code = (
        "import django; django.setup()\n"
        "from graphene_django.views import GraphQLView\n"
        "from tracepanel.core.patching import patched\n"
        "print(patched(GraphQLView, 'execute_graphql_request'))"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "tests.project.settings", "TRACEPANEL_GRAPHENE_ENABLED": enabled}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)  # noqa: S603

    assert result.stdout.strip() == patched
