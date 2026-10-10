import pytest
from dirty_equals import IsInt, IsList, IsPartialDict, IsStr
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore
from tracepanel.core import explain as plans
from tracepanel.core.model import Attrs, Scalar, Trace
from tracepanel.django import explain

SQLITE = pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite plan text")
POSTGRES = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL plan text")


def sql_spans(traces: list[Trace]) -> list[Attrs]:
    (trace,) = traces
    return [span.attrs | {"id": span.id} for span in trace.spans if span.kind == "sql"]


@pytest.mark.django_db
def test_parameters_are_captured_for_explain(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/n-plus-one/")

    assert IsPartialDict({"db.statement": IsStr(regex=r"^SELECT .* WHERE .*%s.*"), "db.params": [IsInt]}) in [
        span for span in sql_spans(traces) if "db.params" in span
    ]


@pytest.mark.django_db
def test_parameters_can_be_left_out(client: Client, traces: list[Trace], users: list[User], settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_sql_params": False}

    client.get("/n-plus-one/")

    assert [span for span in sql_spans(traces) if "db.params" in span] == []


@pytest.mark.django_db
def test_explain_api(client: Client, traces: list[Trace], users: list[User], store: AgentStore) -> None:
    client.get("/n-plus-one/")
    store.write(traces)
    span = next(span for span in sql_spans(traces) if "db.params" in span)

    response = client.get(f"/__tracepanel__/api/explain?trace={traces[0].id}&span={span['id']}")

    assert response.status_code == 200
    assert response.json() == {
        "vendor": connection.vendor,
        "alias": "default",
        "analyzed": False,
        "lines": IsList(length=(1, ...)),
    }
    assert client.get(f"/__tracepanel__/api/explain?trace={traces[0].id}&span=nope").status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("statement", "params", "message"),
    [
        ("UPDATE auth_user SET username = %s", ["x"], "Only SELECT statements can be explained."),
        ("SELECT * FROM auth_user WHERE id = %s", None, "Parameters of this query were not captured"),
        ("SELECT * FROM missing_table", [], "The database could not explain this query"),
    ],
)
def test_explain_refuses(statement: str, params: list[Scalar] | None, message: str) -> None:
    with pytest.raises(plans.ExplainError, match=message):
        explain.explain(statement, params, "default")


@SQLITE
@pytest.mark.django_db
def test_sqlite_plan_flags_full_scans() -> None:
    by_index = explain.explain("SELECT * FROM auth_user WHERE id = %s", [1], "default")
    scan = explain.explain("SELECT * FROM auth_user WHERE first_name = %s ORDER BY last_name", ["Ann"], "default")

    assert by_index.to_dict() == {
        "vendor": "sqlite",
        "alias": "default",
        "analyzed": False,
        "lines": [{"depth": 0, "text": "SEARCH auth_user USING INTEGER PRIMARY KEY (rowid=?)", "warning": None}],
    }
    assert scan.to_dict()["lines"] == [
        {"depth": 0, "text": "SCAN auth_user", "warning": "Full scan of auth_user: no index is used"},
        {
            "depth": 0,
            "text": "USE TEMP B-TREE FOR ORDER BY",
            "warning": "Sorts or groups in a temporary b-tree: no index matches ORDER BY / GROUP BY",
        },
    ]


@POSTGRES
@pytest.mark.django_db
def test_postgres_plan_and_analyze() -> None:
    plan = explain.explain("SELECT * FROM auth_user WHERE first_name = %s", ["Ann"], "default", analyze=True)

    assert plan.to_dict() == {
        "vendor": "postgresql",
        "alias": "default",
        "analyzed": True,
        "lines": IsList(
            {
                "depth": 0,
                "text": IsStr(regex=r"Seq Scan on auth_user .*actual time.*"),
                "warning": "Sequential scan of auth_user: no index is used",
            },
            *[IsPartialDict(depth=IsInt)] * (len(plan.lines) - 1),
        ),
    }
