import re

import pytest
from django.contrib.auth.models import User
from django.test import Client

from tracepanel.core.analysis import analyze, fingerprint, headline, issue_key
from tracepanel.core.ids import new_id
from tracepanel.core.model import Attrs, Span, Trace
from tracepanel.core.sql import span_name


def test_fingerprint_collapses_whitespace_and_in_lists() -> None:
    assert fingerprint('SELECT *\n  FROM "t"  WHERE id IN (%s, %s, %s)') == 'SELECT * FROM "t" WHERE id IN (...)'
    assert fingerprint("SELECT 1 WHERE id IN (%s)") == fingerprint("SELECT 1 WHERE id IN (%s,%s)")


def test_issue_key_is_stable_and_short() -> None:
    assert issue_key("n+1", "SELECT 1", "a.py in f") == issue_key("n+1", "SELECT 1", "a.py in f")
    assert issue_key("n+1", "SELECT 1", "a.py in f") != issue_key("duplicates", "SELECT 1", "a.py in f")
    assert re.fullmatch(r"[0-9a-f]{16}", issue_key("error", "x"))


def test_span_names_tell_counts_from_reads() -> None:
    assert span_name('SELECT "t"."id" FROM "t" WHERE "t"."id" = %s') == "SELECT t"
    assert span_name('SELECT COUNT(*) AS "__count" FROM "t"') == "SELECT COUNT t"
    assert span_name('UPDATE "t" SET "likes" = ("t"."likes" + %s)') == "UPDATE t"


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
    assert analyze(framework) == []

    project = _trace(_sql(count, "a", "app/views.py:3 in v"), _sql(count, "a", "app/views.py:3 in v"))
    assert [i.kind for i in analyze(project)] == ["duplicates"]


def test_n_plus_one_run_by_a_library_is_the_views() -> None:
    by_pk = 'SELECT * FROM "auth_user" WHERE "auth_user"."id" = %s'
    nested_serializer = _trace(*(_sql(by_pk, str(i), None) for i in range(4)))
    nested_serializer.attrs["code.view"] = "blog.views.PostViewSet"
    repeated_count = _trace(*(_sql('SELECT COUNT(*) FROM "t"', str(i), None) for i in range(4)))
    repeated_count.attrs["code.view"] = "django.contrib.admin.options.changelist_view"

    assert [(i.kind, i.culprit) for i in analyze(nested_serializer)] == [
        ("n+1", "blog.views.PostViewSet (in library code)")
    ]
    # Not a by-key lookup: still Django's own business.
    assert analyze(repeated_count) == []


def test_issue_fingerprint_survives_line_changes() -> None:
    statement = 'SELECT * FROM "t" WHERE "t"."id" = %s'
    before = analyze(_trace(*(_sql(statement, str(i), "app/views.py:3 in v") for i in range(3))))
    after = analyze(_trace(*(_sql(statement, str(i), "app/views.py:30 in v") for i in range(3))))
    assert before[0].kind == "n+1"
    assert before[0].fingerprint == after[0].fingerprint


def test_same_statement_on_two_databases_is_not_a_duplicate() -> None:
    count = 'SELECT COUNT(*) FROM "t"'
    spans = [_sql(count, "a", "app/views.py:3 in v"), _sql(count, "a", "app/views.py:4 in v")]
    spans[1].attrs["db.alias"] = "analytics"
    assert analyze(_trace(*spans)) == []


@pytest.mark.django_db
def test_same_related_object_in_a_loop_is_an_n_plus_one(client: Client, traces: list[Trace], users: list[User]) -> None:
    client.get("/same-group/")
    (trace,) = traces

    assert [(issue.kind, issue.count) for issue in trace.issues] == [("n+1", 5)]


@pytest.mark.parametrize(
    ("message", "title"),
    [
        ("division by zero", "division by zero"),
        # A driver's message: the first line is the error, the rest is detail per connection attempt.
        (
            (
                'connection failed: FATAL:  password authentication failed for user "shop"\n'
                "Multiple connection attempts failed. All failures were:\n- host: '127.0.0.1'"
            ),
            'connection failed: FATAL: password authentication failed for user "shop"',
        ),
        ("\n\n  first line after blanks\nsecond", "first line after blanks"),
        ("word " * 60, ("word " * 39).strip() + "…"),
    ],
)
def test_error_titles_are_one_line_cut_at_a_word(message: str, title: str) -> None:
    assert headline(message) == title
