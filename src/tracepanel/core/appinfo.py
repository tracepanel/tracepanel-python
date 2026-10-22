"""What the app is, for the console's App tab: its settings, masked, the versions it runs on, and what is wrong with it.

Per process, not per request: the hello to the agent carries it (tracepanel.storage.agent), worked out once, at the
first connection. A framework says where its settings are (use_settings: Django's settings, Flask's app.config) and
which packages it knows the app runs (add_packages: Django's INSTALLED_APPS); the versions are Python's, the
framework's and those of the packages below that are installed.

What is wrong with it (add_checks: Django's system checks, its migrations not applied or not made) takes longer, and
may wait on the database: it is worked out once, in a thread of its own started at the first connection. Once known,
the hellos after carry it, and the connection open then is told (when_checked: an `{"app": {...}}` frame).
"""

import contextlib
import platform
import threading
from collections.abc import Callable, Iterable, Mapping
from importlib import metadata

from tracepanel.core import conf, forks, runtime, values

# The packages worth a version line when installed: the frameworks and servers, the ORM and database drivers, the
# task queues, the API libraries, tracepanel itself.
PACKAGES = (
    "tracepanel",
    "django",
    "flask",
    "fastapi",
    "starlette",
    "falcon",
    "werkzeug",
    "jinja2",
    "sqlalchemy",
    "flask-sqlalchemy",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
    "asyncpg",
    "mysqlclient",
    "pymysql",
    "mysql-connector-python",
    "aiomysql",
    "asyncmy",
    "oracledb",
    "aiosqlite",
    "pymongo",
    "redis",
    "celery",
    "kombu",
    "rq",
    "dramatiq",
    "channels",
    "daphne",
    "uvicorn",
    "gunicorn",
    "granian",
    "hypercorn",
    "djangorestframework",
    "django-ninja",
    "strawberry-graphql",
    "pydantic",
    "django-tasks",
    "django-q2",
    "opentelemetry-sdk",
    "httpx",
    "requests",
    "aiohttp",
)

Version = dict[str, str]

_settings: Callable[[], Mapping[str, object]] | None = None
_packages: list[Callable[[], Iterable[str]]] = []
_described: dict[str, object] | None = None
_checks: list[Callable[[], Mapping[str, object]]] = []
_listeners: list[Callable[[dict[str, object]], None]] = []
# What the checks found once they ran (None until then); whether they started, in this process.
_checked: dict[str, object] | None = None
_checking = False


def use_settings(source: Callable[[], Mapping[str, object]]) -> None:
    """Where the app's settings are: read once, at the first connection to the agent."""
    global _settings, _described  # noqa: PLW0603 (one app per process)
    _settings = source
    _described = None


def add_packages(source: Callable[[], Iterable[str]]) -> None:
    """More distributions worth a version line (Django: those its INSTALLED_APPS come from)."""
    global _described  # noqa: PLW0603
    if source not in _packages:
        _packages.append(source)
    _described = None


def add_checks(source: Callable[[], Mapping[str, object]]) -> None:
    """What is wrong with the app, as fields of the hello's `app` (Django: `checks`, `migrations`).

    Run once, off the request path, from the first connection on (check()).
    """
    if source not in _checks:
        _checks.append(source)


def when_checked(listener: Callable[[dict[str, object]], None]) -> None:
    """Called with what the checks found once they ran: the agent's connection tells it (tracepanel.storage.agent)."""
    if listener not in _listeners:
        _listeners.append(listener)


def reset() -> None:
    """Forget what was worked out: the next connection works it out again, the checks included."""
    global _described, _checked, _checking  # noqa: PLW0603
    _described = None
    _checked = None
    _checking = False


def _forked() -> None:
    """A forked child has no checking thread: checks that hadn't finished run again in it."""
    global _checking  # noqa: PLW0603
    if _checked is None:
        _checking = False


forks.after_fork(_forked)


def _run_checks() -> None:
    global _checked  # noqa: PLW0603
    found: dict[str, object] = {}
    for source in _checks:
        with contextlib.suppress(Exception):  # a check that fails says nothing; the others still do
            found.update(source())
    _checked = found
    for listener in _listeners:
        with contextlib.suppress(Exception):
            listener(found)


def check() -> None:
    """Run the checks, once per process, in a thread of their own: they may wait on the database."""
    global _checking  # noqa: PLW0603
    if _checking or not _checks:
        return
    _checking = True
    threading.Thread(target=_run_checks, name="tracepanel-checks", daemon=True).start()


def checked() -> dict[str, object] | None:
    """What the checks found, once they ran."""
    return _checked


def _python() -> Version:
    implementation = platform.python_implementation()
    name = "Python" if implementation == "CPython" else implementation
    return {"name": name, "version": platform.python_version(), "kind": "runtime"}


def _installed(name: str) -> Version | None:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    return {"name": str(distribution.metadata["Name"] or name), "version": distribution.version, "kind": "package"}


def versions() -> list[Version]:
    """Python, the framework, then the packages that matter, by name: each with its version."""
    found: list[Version] = [_python()]
    framework = runtime.framework()
    skip = set()
    if framework is not None:
        name, version = framework
        found.append({"name": name, "version": version or "", "kind": "framework"})
        skip.add(runtime.DISTRIBUTIONS.get(name, name).lower())
    wanted = list(PACKAGES)
    for source in _packages:
        try:
            wanted += list(source())
        except Exception:  # noqa: BLE001,S112 (a version list is no reason to fail a connection)
            continue
    packages: dict[str, Version] = {}
    for name in wanted:
        key = name.lower().replace("_", "-")
        if key in skip or key in packages:
            continue
        installed = _installed(name)
        if installed is not None:
            packages[key] = installed
    return found + sorted(packages.values(), key=lambda package: package["name"].lower())


def describe(config: conf.Config) -> dict[str, object]:
    """The hello's `app`: the settings (unless capture_settings is off) and the versions, worked out once.

    And what the checks found, once they ran: the first call starts them.
    """
    global _described  # noqa: PLW0603
    check()
    if _described is None:
        described: dict[str, object] = {}
        if config.capture_settings and _settings is not None:
            try:
                described["settings"] = values.settings_reprs(_settings())
            except Exception:  # noqa: BLE001 (settings that can't be read are left out, the connection goes on)
                described.pop("settings", None)
        try:
            described["versions"] = versions()
        except Exception:  # noqa: BLE001
            described.pop("versions", None)
        _described = described
    described = {**_described, **(_checked or {})}
    if not config.capture_settings:
        return {key: value for key, value in described.items() if key != "settings"}
    return described
