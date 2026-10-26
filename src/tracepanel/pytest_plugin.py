"""tracepanel's pytest plugin: each test a trace, and what the agent finds in it (an N+1, duplicate queries) a failure.

    pytest --tracepanel                       # or `tracepanel = true` in the pytest settings

    [tool.pytest.ini_options]
    tracepanel = true
    tracepanel_fail_on = n+1 duplicates       # the default; slow-sql, error, ... (tracepanel.testing.KINDS)

    @pytest.mark.tracepanel(max_queries=10, allow=["n+1"])
    def test_feed(client, tracepanel_trace):
        client.get("/feed/")
        assert tracepanel_trace.queries <= 10

Installed with tracepanel, the plugin is loaded by pytest and does nothing until turned on: a test run of a project that
has tracepanel should not change by itself. On, it turns tracepanel on as `tracepanel run` does (Django gets
tracepanel.django, an app its middleware, the libraries their integrations), with the widget and the terminal lines
off, and starts the project's agent unless one answers. Each test's body is then a trace (tracepanel.testing),
its fixtures outside it: what runs outside a test's body (a fixture's requests, the test database's `migrate`) is not
sent at all. Once the test has passed, the agent is asked what it found in the test's trace and in its requests': a
finding of a kind it fails on fails the test, with the finding, its query, where the code made it and a link to the
trace in the console. Without an agent (none for the platform,
`agent = "off"`) the tests run as usual, and a single warning says that findings are not checked: counting queries
needs no agent.

Each pytest-xdist worker records its own tests; the agent is the one for all of them. An async test (anyio,
pytest-asyncio) has its trace in the coroutine itself: a runner's event loop can run every test in one task of its own.
"""

import contextlib
import functools
import inspect
import sys
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import pytest

from tracepanel import testing
from tracepanel._compat import override
from tracepanel.core import conf
from tracepanel.core.context import TEST
from tracepanel.core.frames import relative
from tracepanel.core.model import Trace
from tracepanel.storage import agent, transport
from tracepanel.storage.transport import Event
from tracepanel.testing import Console, TracedTest

DEFAULT_FAIL_ON = ("n+1", "duplicates")
MARK = "tracepanel"
MARK_ARGS = frozenset({"max_queries", "fail_on", "allow"})
ON = "tracepanel"
FAIL_ON = "tracepanel_fail_on"


@dataclass(frozen=True, slots=True)
class Session:
    """What the plugin, once on, knows for the whole run: the agent's console (None without one), what fails."""

    console: Console | None
    fail_on: frozenset[str]


SESSION = pytest.StashKey[Session]()
RUN = pytest.StashKey[TracedTest]()


class _SyncWriter(agent.AgentWriter):
    """Sends each trace as it completes, before the code goes on: a test's findings are asked for as soon as it ends.

    Only the tests' traces and what completes while one runs: a fixture's requests, the test database's `migrate`
    and `flush` are the test run's, not a test's.
    """

    @override
    def submit(self, trace: Trace) -> None:
        if trace.kind != TEST and not testing.running():
            return
        # Never breaks the test: a trace that can't be sent is dropped, as the writer thread drops it.
        try:
            self.write([trace])
        except Exception:  # noqa: BLE001
            self.dropped += 1

    @override
    def submit_event(self, event: Event) -> None:
        with contextlib.suppress(Exception):
            self.write_events([event])


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("tracepanel")
    group.addoption(
        "--tracepanel",
        action="store_true",
        default=False,
        help="Run each test as a trace: what the agent finds in it (an N+1, duplicate queries) fails the test.",
    )
    parser.addini(ON, type="bool", default=False, help="Turn tracepanel's plugin on, as --tracepanel does.")
    parser.addini(
        FAIL_ON,
        type="args",
        default=list(DEFAULT_FAIL_ON),
        help=f"The kinds of findings that fail a test (default: {' '.join(DEFAULT_FAIL_ON)}).",
    )


def _on(config: pytest.Config) -> bool:
    return bool(config.getoption("tracepanel") or config.getini(ON))


def kinds(value: object, where: str) -> frozenset[str]:
    """The kinds named: a list of them, or one; an unknown kind is a typo, and an error."""
    names = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set)) else None
    if names is None or not all(isinstance(name, str) for name in names):
        msg = f"{where}: a kind or a list of kinds, got {value!r}"
        raise pytest.UsageError(msg)
    unknown = sorted(set(names) - set(testing.KINDS))
    if unknown:
        msg = f"{where}: unknown kind {', '.join(unknown)} (the kinds: {', '.join(testing.KINDS)})"
        raise pytest.UsageError(msg)
    return frozenset(names)


_activated = False


def _activate() -> None:
    """Turn tracepanel on in this process as `tracepanel run` does, before the tests import the app."""
    global _activated  # noqa: PLW0603 (once per process)
    if _activated:
        return
    _activated = True
    from tracepanel import _run  # noqa: PLC0415 (only once on)

    # configure() wins over the framework's own switch (DEBUG), a TRACEPANEL_* variable still over it. The widget would
    # change the pages the tests read; the terminal lines would come between pytest's.
    conf.configure(enabled=True, widget=False, terminal_summary=False)
    _run.install()
    testing.install()


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """On before pytest-django sets Django up: its settings get tracepanel.django as they load."""
    if _on(early_config):
        _activate()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{MARK}(max_queries=None, fail_on=None, allow=()): with tracepanel's plugin on, fail the test when it makes "
        "more queries than max_queries, or when the agent finds an issue of a kind in fail_on (the "
        f"{FAIL_ON} setting by default) that isn't in allow.",
    )
    if not _on(config):
        return
    _activate()
    fail_on = kinds(config.getini(FAIL_ON), FAIL_ON)
    settings = conf.get_config()
    console = None
    # xdist's workers find the agent the main process started, and say nothing of a missing one: it said so once.
    worker = hasattr(config, "workerinput")
    if _agent_answers(settings, start=not worker):
        url = agent.console_url(settings)
        console = Console(url) if url else None
    elif not worker:
        why = agent.unavailable(settings)
        config.issue_config_time_warning(
            pytest.PytestWarning(f"tracepanel: {why} The tests run without the agent's findings."), stacklevel=2
        )
    config.stash[SESSION] = Session(console, fail_on)


def _agent_answers(settings: conf.Config, *, start: bool) -> bool:
    """The project's agent, started now unless one answers, as `tracepanel run` would: not under pytest otherwise."""
    if agent.answers(agent.address(settings)):
        return True
    if start and settings.agent == "auto":
        agent.start(settings)
    return agent.answers(agent.address(settings))


def pytest_sessionstart(session: pytest.Session) -> None:
    if SESSION not in session.config.stash:
        return
    _django()
    # The settings as the framework has them by now (Django's TRACEPANEL): the thresholds the agent finds issues with.
    transport.set_writer(_SyncWriter(conf.get_config()))


def _django() -> None:
    """Django set up before tracepanel could add itself to INSTALLED_APPS (a conftest read the settings): now, then."""
    apps = getattr(sys.modules.get("django.apps"), "apps", None)
    if apps is None or not apps.ready or apps.is_installed("tracepanel.django"):
        return
    from tracepanel import django  # noqa: PLC0415 (Django is there, and set up)

    django.install()


def pytest_report_header(config: pytest.Config) -> list[str] | None:
    state = config.stash.get(SESSION, None)
    if state is None:
        return None
    fails = ", ".join(sorted(state.fail_on)) or "nothing"
    where = f"console {state.console.url}" if state.console else "no agent: findings not checked"
    return [f"tracepanel: each test a trace · fails on {fails} · {where}"]


def _test(item: pytest.Item, state: Session) -> TracedTest:
    """The item's trace-to-be: the one the fixture hands the test, and the one the call records."""
    run = item.stash.get(RUN, None)
    if run is None:
        _, line, _ = item.location
        attrs = {
            "test.case.name": item.nodeid,
            "code.filepath": relative(str(item.path)),
            "code.lineno": (line or 0) + 1,
            "code.function": getattr(item, "originalname", item.name),
        }
        run = item.stash[RUN] = TracedTest(item.nodeid, attrs, state.console)
    return run


@pytest.fixture
def tracepanel_trace(request: pytest.FixtureRequest) -> TracedTest:
    """The test's trace: its queries so far (`.queries`, `.sql`), what the agent found (`.issues`), its `.url`."""
    state = request.config.stash.get(SESSION, None)
    if state is None:
        pytest.fail(
            "tracepanel_trace needs tracepanel's plugin on: pytest --tracepanel, or tracepanel = true", pytrace=False
        )
    return _test(request.node, state)


@dataclass(frozen=True, slots=True)
class Rules:
    fail_on: frozenset[str]
    max_queries: int | None


def rules(item: pytest.Item, state: Session) -> Rules | str:
    """The test's marks, the nearest first (the function's, then its class's and module's), over the settings.

    Marks that say something wrong: what, to fail the test with.
    """
    values: dict[str, object] = {}
    arguments = ", ".join(sorted(MARK_ARGS))
    for mark in item.iter_markers(MARK):
        if mark.args:
            return f"@pytest.mark.{MARK} takes keyword arguments only ({arguments})"
        for key, value in mark.kwargs.items():
            values.setdefault(key, value)
    unknown = sorted(values.keys() - MARK_ARGS)
    if unknown:
        return f"@pytest.mark.{MARK} has no {', '.join(unknown)} ({arguments})"
    limit = values.get("max_queries")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool)):
        return f"max_queries must be a whole number, got {limit!r}"
    try:
        fail_on = kinds(values["fail_on"], "fail_on") if "fail_on" in values else state.fail_on
        allowed = kinds(values.get("allow", ()), "allow")
    except pytest.UsageError as exc:
        return str(exc)
    return Rules(fail_on - allowed, limit)


def _failure(exc: BaseException) -> BaseException | None:
    """The exception as the trace's error: a skip or an xfail is how the test ended, not an error of it."""
    outcomes = (pytest.skip.Exception, pytest.xfail.Exception, pytest.exit.Exception)
    return None if isinstance(exc, outcomes) else exc


def _in_coroutine(function: Callable[..., Any], run: TracedTest) -> Callable[..., Any]:
    """The test's coroutine function, its trace current in the coroutine: whatever task the runner awaits it in."""

    @functools.wraps(function)
    async def traced(*args: object, **kwargs: object) -> object:
        run.start()
        try:
            result = await function(*args, **kwargs)
        except BaseException as exc:
            run.stop(_failure(exc))
            raise
        run.stop()
        return result

    return traced


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    state = item.config.stash.get(SESSION, None)
    if state is None:
        return (yield)
    found = rules(item, state)
    if isinstance(found, str):
        # The test's own marks: its failure, not the run's.
        pytest.fail(found, pytrace=False)
    run = _test(item, state)
    function = getattr(item, "obj", None)
    coroutine = isinstance(item, pytest.Function) and inspect.iscoroutinefunction(function)
    if coroutine:
        item.obj = _in_coroutine(function, run)  # type: ignore[attr-defined]
    else:
        run.start()
    try:
        result = yield
    except BaseException as exc:
        run.stop(_failure(exc))
        run.send()
        raise
    finally:
        if coroutine:
            item.obj = function  # type: ignore[attr-defined]
    run.stop()
    run.send()
    problems = _problems(run, found)
    if problems:
        pytest.fail("\n\n".join(problems), pytrace=False)
    return result


def _problems(run: TracedTest, found: Rules) -> list[str]:
    problems = []
    if found.max_queries is not None and run.queries > found.max_queries:
        problems.append(testing.too_many(run.sql, found.max_queries))
    if found.fail_on and run.console is not None:
        failing = [finding for finding in run.console.findings(run.traces) if finding.kind in found.fail_on]
        if failing:
            own = run.trace.id if run.trace is not None else None
            listed = "\n\n".join(finding.describe(own) for finding in failing)
            allow = ", ".join(f'"{kind}"' for kind in sorted({finding.kind for finding in failing}))
            problems.append(
                f"tracepanel found {len(failing)} {'issue' if len(failing) == 1 else 'issues'} in this test:\n\n"
                f"{listed}\n\nThe test's trace: {run.url}\n"
                f"Allowed here with @pytest.mark.{MARK}(allow=[{allow}])."
            )
    return problems
