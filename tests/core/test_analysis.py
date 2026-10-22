import re

import pytest
from dirty_equals import IsStr
from django.contrib.auth.models import User
from django.test import Client

from tests.agentstore import issues
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Error, Span, Trace
from tracepanel.core.sql import fingerprint, span_name


def test_fingerprint_collapses_whitespace_and_in_lists() -> None:
    assert fingerprint('SELECT *\n  FROM "t"  WHERE id IN (%s, %s, %s)') == 'SELECT * FROM "t" WHERE id IN (...)'
    assert fingerprint("SELECT 1 WHERE id IN (%s)") == fingerprint("SELECT 1 WHERE id IN (%s,%s)")

    # pyodbc's and sqlite3's ?, and the named placeholders SQLAlchemy gives each value on pymssql and psycopg2.
    assert fingerprint("SELECT 1 WHERE id IN (?, ?)") == "SELECT 1 WHERE id IN (...)"
    assert fingerprint("SELECT 1 WHERE id IN (%(id_1_1)s, %(id_1_2)s)") == "SELECT 1 WHERE id IN (...)"


def test_issue_fingerprints_are_short_and_stable() -> None:
    statement = 'SELECT * FROM "t" WHERE "t"."id" = %s'
    loop = [_sql(statement, str(i), "app/views.py:3 in v") for i in range(3)]

    (first,) = issues(_trace(*loop))
    (again,) = issues(_trace(*loop))

    assert (first.fingerprint, again.fingerprint) == (IsStr(regex="[0-9a-f]{16}"), first.fingerprint)


def test_span_names_tell_counts_from_reads() -> None:
    assert span_name('SELECT "t"."id" FROM "t" WHERE "t"."id" = %s') == "SELECT t"
    assert span_name('SELECT COUNT(*) AS "__count" FROM "t"') == "SELECT COUNT t"
    assert span_name('UPDATE "t" SET "likes" = ("t"."likes" + %s)') == "UPDATE t"


_USERS = "[auth_user].[id] AS [id], [auth_user].[username] AS [username] FROM [auth_user]"
_IN_GROUP = (
    "INNER JOIN [auth_user_groups] ON ([auth_user].[id] = [auth_user_groups].[user_id]) "
    "INNER JOIN [auth_group] ON ([auth_user_groups].[group_id] = [auth_group].[id])"
)


@pytest.mark.parametrize(
    ("statement", "name", "fingerprinted"),
    [
        # SQL Server as mssql-django writes it: [names], TOP for a slice from the start, OFFSET ... FETCH for the rest.
        (
            f"SELECT TOP 21 {_USERS} WHERE [auth_user].[id] = %s",
            "SELECT auth_user",
            f"SELECT TOP 21 {_USERS} WHERE [auth_user].[id] = %s",
        ),
        (
            f"SELECT {_USERS} ORDER BY 1 ASC  OFFSET 20 ROWS FETCH FIRST 10 ROWS ONLY",
            "SELECT auth_user",
            f"SELECT {_USERS} ORDER BY 1 ASC OFFSET 20 ROWS FETCH FIRST 10 ROWS ONLY",
        ),
        (
            f"SELECT {_USERS} WHERE [auth_user].[id] IN (%s, %s, %s)",
            "SELECT auth_user",
            f"SELECT {_USERS} WHERE [auth_user].[id] IN (...)",
        ),
        (
            f"SELECT {_USERS} {_IN_GROUP} WHERE [auth_group].[name] = %s",
            "SELECT auth_user",
            f"SELECT {_USERS} {_IN_GROUP} WHERE [auth_group].[name] = %s",
        ),
        (
            "UPDATE [auth_user] SET [username] = %s WHERE [auth_user].[id] = %s",
            "UPDATE auth_user",
            "UPDATE [auth_user] SET [username] = %s WHERE [auth_user].[id] = %s",
        ),
        # A schema stays in the name, as the agent names a statement it maps from OpenTelemetry.
        (
            "SELECT TOP 10 [o].[id] FROM [dbo].[orders] AS [o] WHERE [o].[customer_id] IN (@P1, @P2, @P3)",
            "SELECT dbo.orders",
            "SELECT TOP 10 [o].[id] FROM [dbo].[orders] AS [o] WHERE [o].[customer_id] IN (...)",
        ),
        (
            "SELECT [id] FROM [dbo].[orders] WHERE [customer_id] IN (@id0, @id1)",
            "SELECT dbo.orders",
            "SELECT [id] FROM [dbo].[orders] WHERE [customer_id] IN (...)",
        ),
        (
            "SELECT id FROM dbo.orders WHERE customer_id IN (?, ?)",
            "SELECT dbo.orders",
            "SELECT id FROM dbo.orders WHERE customer_id IN (...)",
        ),
        # Oracle, PostgreSQL, MySQL and SQLite quoting and placeholders.
        (
            'SELECT "ID" FROM "SHOP"."ORDERS" WHERE "ID" IN (:1, :2)',
            "SELECT SHOP.ORDERS",
            'SELECT "ID" FROM "SHOP"."ORDERS" WHERE "ID" IN (...)',
        ),
        (
            "SELECT id FROM orders WHERE id IN (:id_1, :id_2)",
            "SELECT orders",
            "SELECT id FROM orders WHERE id IN (...)",
        ),
        (
            'SELECT "shop_order"."id" FROM "shop_order" WHERE "shop_order"."id" IN ($1, $2)',
            "SELECT shop_order",
            'SELECT "shop_order"."id" FROM "shop_order" WHERE "shop_order"."id" IN (...)',
        ),
        (
            'SELECT "t"."id" FROM "public"."t" WHERE "t"."id" IN (%s, %s)',
            "SELECT public.t",
            'SELECT "t"."id" FROM "public"."t" WHERE "t"."id" IN (...)',
        ),
        (
            "SELECT `shop_order`.`id` FROM `shop_order` WHERE `shop_order`.`id` IN (%s, %s) LIMIT 21",
            "SELECT shop_order",
            "SELECT `shop_order`.`id` FROM `shop_order` WHERE `shop_order`.`id` IN (...) LIMIT 21",
        ),
        (
            "SELECT id FROM t WHERE id IN (?1, ?2)",
            "SELECT t",
            "SELECT id FROM t WHERE id IN (...)",
        ),
    ],
)
def test_names_and_fingerprints_in_each_dialect(statement: str, name: str, fingerprinted: str) -> None:
    assert (span_name(statement), fingerprint(statement)) == (name, fingerprinted)


def test_ids_are_ulids_sorted_by_time() -> None:
    ids = [new_id() for _ in range(100)]
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", i) for i in ids)
    assert ids[0][:10] <= ids[-1][:10]


def _sql(statement: str, params_hash: str, origin: str | None) -> Span:
    attrs: Attrs = {"db.fingerprint": statement, "db.params_hash": params_hash, "code.origin": origin}
    return Span(id=new_id(), parent_id=None, kind="sql", name="SELECT t", start=0, duration=10, attrs=attrs)


def _trace(*spans: Span) -> Trace:
    return Trace(id=new_id(), kind="http", name="GET /", start=0, spans=list(spans))


def test_only_project_code_patterns_are_issues() -> None:
    count = 'SELECT COUNT(*) FROM "t"'
    framework = _trace(_sql(count, "a", None), _sql(count, "a", None), _sql("BEGIN", "b", "x.py:1 in f"))
    assert issues(framework) == []

    project = _trace(_sql(count, "a", "app/views.py:3 in v"), _sql(count, "a", "app/views.py:3 in v"))
    assert [i.kind for i in issues(project)] == ["duplicates"]


def test_n_plus_one_run_by_a_library_is_the_views() -> None:
    by_pk = 'SELECT * FROM "auth_user" WHERE "auth_user"."id" = %s'
    nested_serializer = _trace(*(_sql(by_pk, str(i), None) for i in range(4)))
    nested_serializer.attrs["code.view"] = "blog.views.PostViewSet"
    repeated_count = _trace(*(_sql('SELECT COUNT(*) FROM "t"', str(i), None) for i in range(4)))
    repeated_count.attrs["code.view"] = "django.contrib.admin.options.changelist_view"

    assert [(i.kind, i.culprit) for i in issues(nested_serializer)] == [
        ("n+1", "blog.views.PostViewSet (in library code)")
    ]
    # Not a by-key lookup: still Django's own business.
    assert issues(repeated_count) == []


def test_issue_fingerprint_survives_line_changes() -> None:
    statement = 'SELECT * FROM "t" WHERE "t"."id" = %s'
    before = issues(_trace(*(_sql(statement, str(i), "app/views.py:3 in v") for i in range(3))))
    after = issues(_trace(*(_sql(statement, str(i), "app/views.py:30 in v") for i in range(3))))
    assert before[0].kind == "n+1"
    assert before[0].fingerprint == after[0].fingerprint


def test_same_statement_on_two_databases_is_not_a_duplicate() -> None:
    count = 'SELECT COUNT(*) FROM "t"'
    spans = [_sql(count, "a", "app/views.py:3 in v"), _sql(count, "a", "app/views.py:4 in v")]
    spans[1].attrs["db.alias"] = "analytics"
    assert issues(_trace(*spans)) == []


@pytest.mark.django_db
def test_same_related_object_in_a_loop_is_an_n_plus_one(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/same-group/")
    (trace,) = traces

    assert [(issue.kind, issue.count) for issue in issues(trace)] == [("n+1", 5)]


@pytest.mark.parametrize(
    ("message", "title"),
    [
        ("division by zero", "ValueError: division by zero"),
        # A driver's message: the first line is the error, the rest is detail per connection attempt.
        (
            (
                'connection failed: FATAL:  password authentication failed for user "shop"\n'
                "Multiple connection attempts failed. All failures were:\n- host: '127.0.0.1'"
            ),
            'ValueError: connection failed: FATAL: password authentication failed for user "shop"',
        ),
        ("\n\n  first line after blanks\nsecond", "ValueError: first line after blanks"),
        ("word " * 60, IsStr(regex=r"ValueError: (word ){30,}word…")),
    ],
)
def test_error_titles_are_one_line_cut_at_a_word(message: str, title: str) -> None:
    trace = _trace()
    trace.errors = [Error(span_id=None, time=0, type="builtins.ValueError", message=message, handled=False, frames=[])]

    assert [(issue.kind, issue.title) for issue in issues(trace)] == [("error", title)]


def test_sql_without_parameters_from_another_sdk() -> None:
    # Go's database/sql through OpenTelemetry: no parameter hash, plain `WHERE id = ?`.
    def go_sql(statement: str) -> Span:
        attrs: Attrs = {"db.fingerprint": statement, "code.origin": "app.go:54 in (*App).listBooks"}
        return Span(id=new_id(), parent_id=None, kind="sql", name="SELECT authors", start=0, duration=10, attrs=attrs)

    lookups = _trace(*(go_sql("SELECT name FROM authors WHERE id = ?") for _ in range(20)))
    counts = _trace(*(go_sql("SELECT COUNT(*) FROM books WHERE author_id = ?") for _ in range(3)))

    assert [(i.kind, i.count) for i in issues(lookups)] == [("n+1", 20)]
    # Without the parameters, repeats can't be called duplicates: they may be a loop over different rows.
    assert issues(counts) == []
