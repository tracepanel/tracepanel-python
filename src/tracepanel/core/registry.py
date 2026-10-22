"""Every integration tracepanel has, as data: what it is called, what library it needs, where its code lives.

One list answers every question about integrations: which to install (tracepanel.django installs all of them, the other
frameworks those that aren't Django apps), which a `tracepanel run` import hook installs, what `tracepanel config` and
the startup line report. An integration is installed only when its `<name>_enabled` setting is on, and only once the
app imports one of its libraries (tracepanel.core.imports): its module's install() does the patching then. A library
the app never imports is never loaded.
"""

import importlib
import importlib.machinery
import importlib.util
import sys
from dataclasses import dataclass
from types import ModuleType

from tracepanel.core import conf, forks, imports

_installed: set[str] = set()
_lock = forks.lock()


@dataclass(frozen=True, slots=True)
class Integration:
    # Its setting is `<name>_enabled` (TRACEPANEL_<NAME>_ENABLED).
    name: str
    label: str
    # Its libraries: any of these importable means one is installed, and importing one installs the integration.
    modules: tuple[str, ...]
    # The module whose install() patches the library.
    code: str
    # A Django app: installed by tracepanel.django only (Django may be in the environment of a FastAPI project too).
    django_only: bool = False

    @property
    def available(self) -> bool:
        return any(_installed_module(module) for module in self.modules)

    @property
    def enabled(self) -> bool:
        enabled: bool = getattr(conf.get_config(), f"{self.name}_enabled")
        return enabled

    def install(self) -> None:
        """Patch the library once the app imports it (now, when it has), unless the setting turns it off."""
        if self.enabled:
            for module in self.modules:
                imports.when_imported(module, self._load)

    def _load(self, module: ModuleType) -> None:
        del module
        with _lock:
            if self.name in _installed:
                return
            _installed.add(self.name)
        importlib.import_module(self.code).install()


def _installed_module(name: str) -> bool:
    """Whether the module can be imported, without importing it or its parent packages (find_spec would)."""
    if name in sys.modules:
        return True
    top, *rest = name.split(".")
    spec = importlib.machinery.PathFinder.find_spec(top) or importlib.util.find_spec(top)
    for part in rest:
        if spec is None or spec.submodule_search_locations is None:
            return False
        spec = importlib.machinery.PathFinder.find_spec(part, list(spec.submodule_search_locations))
    return spec is not None


INTEGRATIONS: tuple[Integration, ...] = (
    Integration("logging", "logging", ("logging",), "tracepanel.integrations.logging"),
    Integration("sqlalchemy", "SQLAlchemy", ("sqlalchemy",), "tracepanel.integrations.sqlalchemy"),
    Integration(
        "dbapi",
        "Database drivers",
        (
            "sqlite3",
            "aiosqlite",
            "psycopg",
            "psycopg2",
            "asyncpg",
            "pymysql",
            "aiomysql",
            "asyncmy",
            "MySQLdb",
            "mysql.connector",
            "oracledb",
            "pyodbc",
            "pymssql",
        ),
        "tracepanel.integrations.dbapi",
    ),
    Integration(
        "http_clients",
        "HTTP clients",
        ("urllib3", "httpx", "httpx2", "aiohttp"),
        "tracepanel.integrations.http_clients",
    ),
    Integration("redis", "Redis", ("redis", "valkey"), "tracepanel.integrations.redis"),
    Integration("mongodb", "MongoDB", ("pymongo",), "tracepanel.integrations.mongodb"),
    Integration("celery", "Celery", ("celery",), "tracepanel.integrations.celery"),
    Integration("dramatiq", "Dramatiq", ("dramatiq",), "tracepanel.integrations.dramatiq"),
    Integration("rq", "RQ", ("rq",), "tracepanel.integrations.rq"),
    Integration("huey", "Huey", ("huey",), "tracepanel.integrations.huey"),
    Integration("faststream", "FastStream", ("faststream",), "tracepanel.integrations.faststream"),
    Integration("strawberry", "Strawberry", ("strawberry",), "tracepanel.integrations.strawberry"),
    Integration("jinja", "Jinja", ("jinja2",), "tracepanel.integrations.jinja"),
    Integration("otel", "OpenTelemetry", ("opentelemetry.sdk",), "tracepanel.integrations.otel"),
    Integration(
        "django_tasks", "Django Tasks", ("django.tasks", "django_tasks"), "tracepanel.django.tasks", django_only=True
    ),
    Integration("django_q", "django-q2", ("django_q",), "tracepanel.django.djangoq", django_only=True),
    Integration("channels", "Channels", ("channels",), "tracepanel.django.channels", django_only=True),
    Integration("drf", "DRF", ("rest_framework",), "tracepanel.django.drf", django_only=True),
    Integration("ninja", "Django Ninja", ("ninja",), "tracepanel.django.ninja", django_only=True),
    Integration("graphene", "Graphene", ("graphene_django",), "tracepanel.django.graphene", django_only=True),
)
BY_NAME = {integration.name: integration for integration in INTEGRATIONS}


def get(name: str) -> Integration:
    return BY_NAME[name]


def install(*, django: bool = False) -> None:
    """Every integration a framework gets: all of them in a Django process, the libraries elsewhere."""
    for integration in INTEGRATIONS:
        if django or not integration.django_only:
            integration.install()


def active(*, django: bool) -> list[Integration]:
    """What a process traces: integrations whose library is installed and whose setting is on."""
    return [i for i in INTEGRATIONS if (django or not i.django_only) and i.available and i.enabled]
