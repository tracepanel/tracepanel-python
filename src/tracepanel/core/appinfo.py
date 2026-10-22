"""What the app is, for the console's App tab: its settings, masked, and the versions it runs on.

Per process, not per request: the hello to the agent carries it (tracepanel.storage.agent), worked out once, at the
first connection. A framework says where its settings are (use_settings: Django's settings, Flask's app.config) and
which packages it knows the app runs (add_packages: Django's INSTALLED_APPS); the versions are Python's, the
framework's and those of the packages below that are installed.
"""

import platform
from collections.abc import Callable, Iterable, Mapping
from importlib import metadata

from tracepanel.core import conf, runtime, values

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
    "huey",
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
    "opentelemetry-sdk",
    "httpx",
    "requests",
    "aiohttp",
)

Version = dict[str, str]

_settings: Callable[[], Mapping[str, object]] | None = None
_packages: list[Callable[[], Iterable[str]]] = []
_described: dict[str, object] | None = None


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


def reset() -> None:
    """Forget what was worked out: the next connection works it out again."""
    global _described  # noqa: PLW0603
    _described = None


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
    """The hello's `app`: the settings (unless capture_settings is off) and the versions; worked out once."""
    global _described  # noqa: PLW0603
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
    if not config.capture_settings:
        return {key: value for key, value in _described.items() if key != "settings"}
    return _described
