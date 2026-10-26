"""The pytest plugin, run on test files of its own (pytester), each run in a process of its own, against the agent."""

import shutil
import subprocess
from collections.abc import Callable

import pytest
from dirty_equals import IsPartialDict, IsStr

from tests.agentstore import AgentStore

# A table and the two ways of reading it: a query per row (an N+1), and one query.
DB = """
import sqlite3

import pytest


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute("CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT)")
    connection.executemany("INSERT INTO author (id, name) VALUES (?, ?)", [(i, f"a{i}") for i in range(5)])
    return connection


def one_by_one(db):
    names = []
    for i in range(5):
        names.append(db.execute("SELECT name FROM author WHERE id = ?", (i,)).fetchone())
    return names


def at_once(db):
    return db.execute("SELECT name FROM author").fetchall()
"""

N_PLUS_ONE = """
from helpers import db, one_by_one


def test_authors(db):
    one_by_one(db)
"""

CLEAN = """
from helpers import at_once, db


def test_authors(db):
    at_once(db)
"""

# A trace in the agent's console.
TRACE_URL = r"http://127\.0\.0\.1:\d+/#/t/[0-9A-Z]{26}"

Run = Callable[..., pytest.RunResult]


@pytest.fixture
def run(pytester: pytest.Pytester, store: AgentStore, monkeypatch: pytest.MonkeyPatch) -> Run:
    """Run pytest in a process of its own, on the files the test writes, sending to the store's agent."""
    # What this run's own pytest-django and xdist leave in the environment is not the inner run's.
    for name in (
        "DJANGO_SETTINGS_MODULE",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.delenv(name, raising=False)
    pytester.makepyfile(helpers=DB)

    def run(*args: str) -> pytest.RunResult:
        return pytester.runpytest_subprocess("-p", "no:cacheprovider", *args)

    return run


def failure(result: pytest.RunResult, test: str) -> list[str]:
    """The lines pytest reports a failed test with, between its header and the next one."""
    lines = result.outlines
    start = next(i for i, line in enumerate(lines) if line.strip("_ ") == test) + 1
    end = next(i for i, line in enumerate(lines[start:], start) if line.startswith(("____", "====")))
    return lines[start:end]


def test_off_unless_turned_on(pytester: pytest.Pytester, run: Run, store: AgentStore) -> None:
    pytester.makepyfile(test_authors=N_PLUS_ONE)

    result = run()

    result.assert_outcomes(passed=1)
    assert store.list_traces() == []


def test_an_n_plus_one_fails_the_test(pytester: pytest.Pytester, run: Run, store: AgentStore) -> None:
    pytester.makepyfile(test_authors=N_PLUS_ONE)

    result = run("--tracepanel")

    result.assert_outcomes(failed=1)
    assert failure(result, "test_authors") == [
        "tracepanel found 1 issue in this test:",
        "",
        "N+1: 5 similar queries · SELECT author",
        "  SELECT name FROM author WHERE id = ?",
        "  at helpers.py:17 in one_by_one",
        IsStr(regex=r"  Load the related rows .*"),
        "",
        IsStr(regex=rf"The test's trace: {TRACE_URL}"),
        'Allowed here with @pytest.mark.tracepanel(allow=["n+1"]).',
    ]

    assert store.list_traces() == [
        IsPartialDict({"kind": "test", "name": "test_authors.py::test_authors", "status": "ok", "queries": 5}),
    ]


def test_a_clean_test_passes(pytester: pytest.Pytester, run: Run, store: AgentStore) -> None:
    pytester.makepyfile(test_authors=CLEAN)

    result = run("--tracepanel")

    result.assert_outcomes(passed=1)
    assert store.list_traces() == [
        IsPartialDict({"kind": "test", "name": "test_authors.py::test_authors", "issues": 0, "queries": 1}),
    ]


def test_the_setting_turns_it_on_and_says_what_fails(pytester: pytest.Pytester, run: Run) -> None:
    pytester.makeini("[pytest]\ntracepanel = true\ntracepanel_fail_on = duplicates slow-sql\n")
    pytester.makepyfile(test_authors=N_PLUS_ONE)

    result = run()

    # An N+1 isn't among the kinds that fail.
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["tracepanel: each test a trace · fails on duplicates, slow-sql · console http://*"])


def test_marks_choose_what_fails(pytester: pytest.Pytester, run: Run) -> None:
    pytester.makepyfile(
        test_marks="""
        import pytest
        from helpers import at_once, db, one_by_one

        pytestmark = pytest.mark.tracepanel(fail_on=["n+1", "duplicates"])


        @pytest.mark.tracepanel(allow="n+1")
        def test_allowed(db):
            one_by_one(db)


        @pytest.mark.tracepanel(fail_on=["slow-sql"])
        def test_other_kinds(db):
            one_by_one(db)


        def test_from_the_module(db):
            at_once(db)
            at_once(db)
            at_once(db)


        @pytest.mark.tracepanel(max_queries=2, fail_on=[])
        def test_max_queries(db):
            at_once(db)
            at_once(db)
            at_once(db)
        """
    )

    result = run("--tracepanel")

    result.assert_outcomes(passed=2, failed=2)
    assert failure(result, "test_from_the_module")[:3] == [
        "tracepanel found 1 issue in this test:",
        "",
        IsStr(regex=r"Duplicate queries: .*"),
    ]

    assert failure(result, "test_max_queries") == [
        "3 queries, at most 2 expected:",
        "  SELECT name FROM author",
        "    at helpers.py:22 in at_once",
        "  SELECT name FROM author",
        "    at helpers.py:22 in at_once",
        "  SELECT name FROM author",
        "    at helpers.py:22 in at_once",
    ]


def test_an_unknown_kind_is_an_error(pytester: pytest.Pytester, run: Run) -> None:
    pytester.makeini("[pytest]\ntracepanel_fail_on = n+1 n_plus_one\n")
    pytester.makepyfile(test_authors=CLEAN)

    result = run("--tracepanel")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*tracepanel_fail_on: unknown kind n_plus_one (the kinds: n+1, duplicates, *"])


def test_a_mark_with_an_unknown_kind_fails_its_test(pytester: pytest.Pytester, run: Run) -> None:
    pytester.makepyfile(
        test_marks="""
        import pytest


        @pytest.mark.tracepanel(allow=["n+2"])
        def test_typo():
            pass


        def test_fine():
            pass
        """
    )

    result = run("--tracepanel")

    result.assert_outcomes(passed=1, failed=1)
    assert failure(result, "test_typo") == [
        (
            "allow: unknown kind n+2 (the kinds: n+1, duplicates, slow-sql, slow-http, slow-call, error, "
            "long-transaction, in-transaction, htmx, queue-wait, blocking-call, large-result)"
        ),
    ]


def test_the_fixture_and_max_queries(pytester: pytest.Pytester, run: Run) -> None:
    pytester.makepyfile(
        test_counts="""
        import tracepanel
        from helpers import at_once, db


        def test_fixture(db, tracepanel_trace):
            at_once(db)
            db.execute("SELECT count(*) FROM author").fetchone()
            assert tracepanel_trace.queries == 2
            assert tracepanel_trace.issues == []
            assert tracepanel_trace.url.startswith("http://127.0.0.1:")


        def test_max_queries(db):
            with tracepanel.max_queries(2):
                at_once(db)
            with tracepanel.max_queries(1):
                at_once(db)
                at_once(db)
        """
    )

    result = run("--tracepanel")

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            ">       with tracepanel.max_queries(1):",
            "E       AssertionError: 2 queries, at most 1 expected:",
            "E         SELECT name FROM author",
            "E           at helpers.py:22 in at_once",
            "E         SELECT name FROM author",
            "E           at helpers.py:22 in at_once",
        ]
    )


def test_async_tests(pytester: pytest.Pytester, run: Run, store: AgentStore) -> None:
    pytester.makepyfile(
        test_async="""
        import pytest
        from helpers import at_once, db, one_by_one


        @pytest.fixture
        async def ready():
            return True


        @pytest.mark.anyio
        async def test_n_plus_one(db, ready):
            one_by_one(db)


        @pytest.mark.anyio
        async def test_clean(db, ready):
            at_once(db)
        """
    )

    result = run("--tracepanel", "-p", "anyio")

    result.assert_outcomes(passed=1, failed=1)
    assert failure(result, "test_n_plus_one[asyncio]")[2] == "N+1: 5 similar queries · SELECT author"

    # The runner awaits every test in a task of its own: each test's queries are still its own.
    assert sorted((trace["name"], trace["queries"]) for trace in store.list_traces()) == [
        ("test_async.py::test_clean[asyncio]", 1),
        ("test_async.py::test_n_plus_one[asyncio]", 5),
    ]


def test_xdist_workers(pytester: pytest.Pytester, run: Run, store: AgentStore) -> None:
    pytester.makepyfile(test_first=N_PLUS_ONE, test_second=CLEAN)

    result = run("--tracepanel", "-n", "2")

    result.assert_outcomes(passed=1, failed=1)
    assert sorted(trace["name"] for trace in store.list_traces()) == [
        "test_first.py::test_authors",
        "test_second.py::test_authors",
    ]


def test_without_an_agent_queries_still_count(
    pytester: pytest.Pytester, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_AGENT", "off")
    pytester.makepyfile(
        test_authors="""
        import pytest
        from helpers import db, one_by_one


        def test_n_plus_one(db):
            one_by_one(db)


        @pytest.mark.tracepanel(max_queries=4)
        def test_max_queries(db):
            one_by_one(db)
        """
    )

    result = run("--tracepanel")

    result.assert_outcomes(passed=1, failed=1)
    assert result.stdout.str().count("PytestWarning: tracepanel: the agent is off") == 1
    result.stdout.fnmatch_lines(["tracepanel: each test a trace · fails on duplicates, n+1 · no agent: *"])


@pytest.mark.skipif(shutil.which("pkill") is None, reason="stops the agent it started with pkill")
def test_starts_the_agent(pytester: pytest.Pytester, run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = pytester.path / ".tracepanel"
    monkeypatch.setenv("TRACEPANEL_DIR", str(directory))
    pytester.makepyfile(test_authors=N_PLUS_ONE)

    try:
        result = run("--tracepanel")
    finally:
        # Started as `tracepanel run` starts it, it would wait half an hour unused before it exits.
        subprocess.run(["pkill", "-f", "--", f"--dir {directory}"], check=False)  # noqa: S603, S607

    result.assert_outcomes(failed=1)
    result.stderr.fnmatch_lines([f"tracepanel ▸ agent started for {directory}"])


DJANGO_SETTINGS = """
SECRET_KEY = "tests"
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ROOT_URLCONF = "urls"
USE_TZ = True
"""

DJANGO_URLS = """
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.urls import path


def users(request):
    groups = []
    for user in User.objects.all():
        groups.append(list(user.groups.values_list("name", flat=True)))
    return JsonResponse({"groups": groups})


urlpatterns = [path("users/", users)]
"""


def test_requests_through_the_test_client_are_traces_of_their_own(
    pytester: pytest.Pytester, run: Run, store: AgentStore
) -> None:
    pytester.makepyfile(settings=DJANGO_SETTINGS, urls=DJANGO_URLS)
    pytester.makepyfile(
        test_views="""
        import pytest
        from django.contrib.auth.models import User


        @pytest.fixture
        def people(db):
            return [User.objects.create(username=f"user{i}") for i in range(5)]


        def test_users(client, people):
            assert client.get("/users/").status_code == 200
        """
    )

    result = run("--tracepanel", "--ds=settings", "-o", "pythonpath=.")

    result.assert_outcomes(failed=1)
    assert failure(result, "test_users")[:6] == [
        "tracepanel found 1 issue in this test:",
        "",
        "N+1: 5 similar queries · SELECT auth_group",
        IsStr(regex=r'  SELECT "auth_group"\."name" .*'),
        "  at urls.py:9 in users",
        IsStr(regex=rf"  in GET /users/ → {TRACE_URL}"),
    ]

    # The fixture's queries are not the test's; the request is a call of the test's trace.
    request, test = sorted(store.list_traces(), key=lambda trace: trace["kind"])
    assert (test["kind"], test["name"], test["queries"]) == ("test", "test_views.py::test_users", 0)
    assert (request["kind"], request["name"], request["queries"]) == ("http", "GET /users/", 6)
    assert store.related(request["id"])["called_by"] == IsPartialDict({"id": test["id"]})


def test_starlette_and_flask_test_clients(pytester: pytest.Pytester, run: Run, store: AgentStore) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("flask")
    pytester.makepyfile(
        test_apps="""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from flask import Flask
        from helpers import db, one_by_one

        api = FastAPI()
        web = Flask(__name__)


        def test_fastapi(db):
            api.get("/authors")(lambda: len(one_by_one(db)))
            assert TestClient(api).get("/authors").status_code == 200


        def test_flask(db):
            web.get("/authors")(lambda: str(len(one_by_one(db))))
            assert web.test_client().get("/authors").status_code == 200
        """
    )

    result = run("--tracepanel")

    result.assert_outcomes(failed=2)
    traces = store.list_traces()
    tests = {trace["name"]: trace["id"] for trace in traces if trace["kind"] == "test"}
    requests = [trace for trace in traces if trace["kind"] == "http"]
    assert [(trace["name"], trace["queries"]) for trace in requests] == [("GET /authors", 5), ("GET /authors", 5)]
    # Each request is a call of its test's trace.
    callers = sorted(store.related(trace["id"])["called_by"]["id"] for trace in requests)
    assert callers == sorted(tests.values())


def test_skipped_and_failed_tests(pytester: pytest.Pytester, run: Run, store: AgentStore) -> None:
    pytester.makepyfile(
        test_outcomes="""
        import pytest
        from helpers import db, one_by_one


        def test_skipped():
            pytest.skip("not today")


        def test_failed(db):
            one_by_one(db)
            assert False, "its own failure"
        """
    )

    result = run("--tracepanel")

    # A test that failed already fails with its own failure, not with what the agent found.
    result.assert_outcomes(skipped=1, failed=1)
    assert "tracepanel found" not in result.stdout.str()

    assert sorted((trace["name"], trace["status"], trace["errors"]) for trace in store.list_traces()) == [
        ("test_outcomes.py::test_failed", "error", 1),
        ("test_outcomes.py::test_skipped", "ok", 0),
    ]


def test_the_fixture_needs_the_plugin_on(pytester: pytest.Pytester, run: Run) -> None:
    pytester.makepyfile(test_fixture="def test_queries(tracepanel_trace):\n    pass\n")

    result = run()

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*tracepanel_trace needs tracepanel's plugin on: pytest --tracepanel*"])


def test_marks_are_registered(pytester: pytest.Pytester, run: Run) -> None:
    pytester.makepyfile(
        test_marks="import pytest\n\n@pytest.mark.tracepanel(max_queries=1)\ndef test_mark():\n    pass\n"
    )

    result = run("--strict-markers")

    result.assert_outcomes(passed=1)
