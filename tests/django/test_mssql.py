"""SQL Server under Django (mssql-django on pyodbc): the `mssql` database of the test project, when there is one."""

import pytest
from dirty_equals import IsPartialDict, IsStr
from django.contrib.auth.models import Group, User
from django.db import connections, transaction

from tests.agentstore import issues
from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.django import explain
from tracepanel.storage.transport import complete

pytestmark = [
    pytest.mark.skipif("mssql" not in connections, reason="needs SQL Server (TRACEPANEL_TEST_MSSQL, mssql-django)"),
    # The default database too: the test project's receivers of Group's signals query it.
    pytest.mark.django_db(databases=["default", "mssql"], transaction=True),
]


def spans(trace: Trace, kind: str) -> list[Span]:
    return [span for span in trace.spans if span.kind == kind]


def test_queries_are_recorded_once_and_repeats_found() -> None:
    Group.objects.using("mssql").bulk_create([Group(name=f"group {n}") for n in range(3)])

    with start_trace("http", "GET /groups") as trace:
        for group in Group.objects.using("mssql").order_by("id"):
            list(Group.objects.using("mssql").filter(name=group.name))
    done = complete(trace)
    queries = spans(done, "sql")

    # Django's statements, once: pyodbc's connection is mssql-django's own, not recorded again.
    assert [(span.attrs["db.client"], span.name) for span in queries] == [("django", "SELECT auth_group")] * 4
    assert queries[1].attrs == IsPartialDict(
        {
            "db.alias": "mssql",
            "db.vendor": "microsoft",
            "db.system": "mssql",
            "db.namespace": IsStr(regex=r".*tracepanel"),
            "db.statement": IsStr(regex=r"^SELECT .* FROM \[auth_group\] WHERE \[auth_group\]\.\[name\] = %s"),
            "db.params": ["group 0"],
            "db.response.returned_rows": 1,
        }
    )
    assert [issue.kind for issue in issues(done)] == ["n+1"]


def test_atomic_blocks_are_transactions() -> None:
    with start_trace("http", "POST /groups") as trace, transaction.atomic(using="mssql"):
        Group.objects.using("mssql").create(name="admins")

    (span,) = spans(complete(trace), "transaction")

    assert span.attrs == IsPartialDict({"db.alias": "mssql", "db.system": "mssql", "db.transaction.outcome": "commit"})


def test_explain_shows_the_estimated_plan_and_sets_the_session_back() -> None:
    User.objects.db_manager("mssql").create_user("ann", first_name="Ann")

    # No index on first_name: the table's clustered index is read whole.
    plan = explain.explain("SELECT * FROM auth_user WHERE first_name = %s", ["Ann"], "mssql")

    assert plan.to_dict() == {
        "vendor": "microsoft",
        "alias": "mssql",
        "analyzed": False,
        "lines": [
            {
                "depth": 0,
                "text": IsStr(
                    regex=r"^Clustered Index Scan\(OBJECT:\(\[\w+\]\.\[dbo\]\.\[auth_user\].*\(rows=.* cost=.*\)$"
                ),
                "warning": "Full scan of auth_user: no index is used",
            }
        ],
    }
    # The connection runs statements again, not their plans.
    assert User.objects.using("mssql").filter(first_name="Ann").count() == 1
