"""What Django finds wrong with the app, for the App tab: its system checks, and its migrations not applied or not made.

What `manage.py check` reports (`check --deploy` too when DEBUG is off), less the silenced ones, and none of the
database checks: they query the database. What `runserver` warns of: the migrations of the default database not
applied yet. And what `makemigrations --check` finds: model changes without a migration. Once per process, in the
checks' own thread (tracepanel.core.appinfo): reading the migrations applied waits on the database, and one that
doesn't answer leaves them out (`unapplied` null), nothing else.
"""

import contextlib
from collections.abc import Iterable

from django.apps import apps
from django.conf import settings
from django.core.checks import CheckMessage, run_checks
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.db.models import Model

from tracepanel.core import appinfo

# Django's levels by their lower bound (django.core.checks.messages): a custom level counts as the one below it.
LEVELS = ((50, "critical"), (40, "error"), (30, "warning"), (20, "info"))
# A project with more is told its first ones: the most serious first.
MAX_CHECKS = 100


def level(number: int) -> str:
    return next((name for bound, name in LEVELS if number >= bound), "debug")


def _object(message: CheckMessage) -> str | None:
    """What the message is about, as `manage.py check` names it: a model by its label (`shop.Order`)."""
    found = message.obj
    if found is None:
        return None
    if isinstance(found, type) and issubclass(found, Model):
        return found._meta.label  # noqa: SLF001 (Django's documented Options API)
    return str(found)


def system_checks() -> list[dict[str, object]]:
    """Django's system checks, the deployment ones too when DEBUG is off: id, level, message, hint, object."""
    messages = [
        message for message in run_checks(include_deployment_checks=not settings.DEBUG) if not message.is_silenced()
    ]
    messages.sort(key=lambda message: (-message.level, message.id or ""))
    return [
        {
            "id": message.id,
            "level": level(message.level),
            "message": str(message.msg),
            "hint": str(message.hint) if message.hint else None,
            "object": _object(message),
        }
        for message in messages[:MAX_CHECKS]
    ]


def _by_app(pairs: Iterable[tuple[str, str]], field: str) -> list[dict[str, object]]:
    grouped: dict[str, list[str]] = {}
    for app, name in pairs:
        grouped.setdefault(app, []).append(name)
    return [{"app": app, field: names} for app, names in grouped.items()]


def unapplied() -> list[dict[str, object]] | None:
    """The default database's migrations not applied yet, by app, in the order migrate applies them.

    None when it can't say: no database configured, or one that doesn't answer.
    """
    connection = connections[DEFAULT_DB_ALIAS]
    if connection.settings_dict.get("ENGINE", "").endswith("dummy"):
        return None
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:  # noqa: BLE001 (a database that doesn't answer: unknown, not an error of the app's)
        return None
    return _by_app(((migration.app_label, migration.name) for migration, _backwards in plan), "names")


def unmade() -> list[dict[str, object]]:
    """Model changes without a migration, by app, as `makemigrations --check` finds them: from the files alone."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    detector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps))
    changes = detector.changes(graph=loader.graph)
    return _by_app(
        (
            (app, operation.describe())
            for app, migrations in changes.items()
            for migration in migrations
            for operation in migration.operations
        ),
        "changes",
    )


def report(*, database: bool = True) -> dict[str, object]:
    """The hello's `checks` and `migrations`: each left out, or null, when working it out failed.

    The migrations applied are read from the database only with `database`.
    """
    found: dict[str, object] = {}
    # A check that raises is no reason to say nothing else.
    with contextlib.suppress(Exception):
        found["checks"] = system_checks()
    try:
        changes: list[dict[str, object]] | None = unmade()
    except Exception:  # noqa: BLE001
        changes = None
    found["migrations"] = {"unapplied": unapplied() if database else None, "changes": changes}
    return found


def _in_memory() -> bool:
    in_memory = getattr(connections[DEFAULT_DB_ALIAS], "is_in_memory_db", None)
    return bool(callable(in_memory) and in_memory())


def in_own_thread() -> dict[str, object]:
    """report(), in the checks' thread: its connections are its own, closed once done.

    An in-memory SQLite database is left alone: this thread's would be another one, new and empty, every migration
    unapplied.
    """
    try:
        return report(database=not _in_memory())
    finally:
        connections.close_all()


def install() -> None:
    appinfo.add_checks(in_own_thread)
