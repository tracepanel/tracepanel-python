"""Every integration tracepanel has, as data: what it is called, what library it needs, where its code lives.

One list answers every question about integrations: which to install (tracepanel.django installs all of them, the other
frameworks those that aren't Django apps), which a `tracepanel run` import hook installs, what `tracepanel config` and
the startup line report. An integration is installed only when its library is importable and its `<name>_enabled`
setting is on; its module's install() does the patching.
"""

import importlib
import importlib.util
from dataclasses import dataclass

from tracepanel.core import conf


@dataclass(frozen=True, slots=True)
class Integration:
    # Its setting is `<name>_enabled` (TRACEPANEL_<NAME>_ENABLED).
    name: str
    label: str
    # Any of these importable means the library is installed.
    modules: tuple[str, ...]
    # The module whose install() patches the library.
    code: str
    # A Django app: installed by tracepanel.django only (Django may be in the environment of a FastAPI project too).
    django_only: bool = False

    @property
    def available(self) -> bool:
        for module in self.modules:
            try:
                if importlib.util.find_spec(module) is not None:
                    return True
            except (ImportError, ValueError):
                # A parent package that isn't there (django.tasks without Django).
                continue
        return False

    @property
    def enabled(self) -> bool:
        enabled: bool = getattr(conf.get_config(), f"{self.name}_enabled")
        return enabled

    def install(self) -> None:
        """Patch the library when it is installed, unless the setting turns it off."""
        if self.enabled and self.available:
            importlib.import_module(self.code).install()


INTEGRATIONS: tuple[Integration, ...] = (
    Integration("logging", "logging", ("logging",), "tracepanel.integrations.logging"),
    Integration("sqlalchemy", "SQLAlchemy", ("sqlalchemy",), "tracepanel.integrations.sqlalchemy"),
    Integration("dbapi", "Database drivers", ("sqlite3",), "tracepanel.integrations.dbapi"),
    Integration(
        "http_clients",
        "HTTP clients",
        ("urllib3", "httpx", "httpx2", "aiohttp"),
        "tracepanel.integrations.http_clients",
    ),
    Integration("redis", "Redis", ("redis", "valkey"), "tracepanel.integrations.redis"),
    Integration("celery", "Celery", ("celery",), "tracepanel.integrations.celery"),
    Integration("dramatiq", "Dramatiq", ("dramatiq",), "tracepanel.integrations.dramatiq"),
    Integration("rq", "RQ", ("rq",), "tracepanel.integrations.rq"),
    Integration("strawberry", "Strawberry", ("strawberry",), "tracepanel.integrations.strawberry"),
    Integration("otel", "OpenTelemetry", ("opentelemetry.sdk",), "tracepanel.integrations.otel"),
    Integration(
        "django_tasks", "Django Tasks", ("django.tasks", "django_tasks"), "tracepanel.django.tasks", django_only=True
    ),
    Integration("channels", "Channels", ("channels",), "tracepanel.django.channels", django_only=True),
    Integration("drf", "DRF", ("rest_framework",), "tracepanel.django.drf", django_only=True),
    Integration("ninja", "Django Ninja", ("ninja",), "tracepanel.django.ninja", django_only=True),
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
