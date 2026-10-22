"""What Django finds wrong with an app, for the App tab: its system checks, its migrations not applied or not made.

A project of its own, in a process of its own: a model with a field its migration doesn't have, and a database with
no migration applied.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from dirty_equals import IsList, IsPartialDict
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from tracepanel.django import checks

MODELS = """
from django.db import models


class Order(models.Model):
    name = models.CharField(max_length=20)
    quantity = models.IntegerField(max_length=3)
    note = models.TextField(default="")
"""
MIGRATION = """
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=20)),
                ("quantity", models.IntegerField(max_length=3)),
            ],
        ),
    ]
"""
SETTINGS = """
import json
import os

DEBUG = os.environ["DEBUG"] == "1"
SECRET_KEY = "tracepanel-tests"
INSTALLED_APPS = ["shop"]
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
DATABASES = {"default": json.loads(os.environ["DATABASE"])}
SILENCED_SYSTEM_CHECKS = json.loads(os.environ.get("SILENCED", "[]"))
"""
# A field's check, the same on every Django version.
W122 = {
    "id": "fields.W122",
    "level": "warning",
    "message": "'max_length' is ignored when used with IntegerField.",
    "hint": "Remove 'max_length' from field",
    "object": "shop.Order.quantity",
}
CHANGES = [{"app": "shop", "changes": ["Add field note to order"]}]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "shop" / "migrations").mkdir(parents=True)
    (tmp_path / "shop" / "__init__.py").touch()
    (tmp_path / "shop" / "migrations" / "__init__.py").touch()
    (tmp_path / "shop" / "models.py").write_text(MODELS)
    (tmp_path / "shop" / "migrations" / "0001_initial.py").write_text(MIGRATION)
    (tmp_path / "settings.py").write_text(SETTINGS)
    return tmp_path


def _run(project: Path, code: str, *, debug: bool = True, database: dict[str, str] | None = None, **env: str) -> str:
    variables = {
        **os.environ,
        "PYTHONPATH": str(project),
        "DJANGO_SETTINGS_MODULE": "settings",
        "DEBUG": "1" if debug else "0",
        "DATABASE": json.dumps(
            database or {"ENGINE": "django.db.backends.sqlite3", "NAME": str(project / "db.sqlite3")}
        ),
        **env,
    }
    setup = "import django, json; django.setup()\nfrom tracepanel.django import checks\n"
    result = subprocess.run(  # noqa: S603 (our own interpreter and test project)
        [sys.executable, "-c", setup + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=variables,
        cwd=project,
        check=True,
    )
    return result.stdout


def _report(project: Path, *, debug: bool = True, database: dict[str, str] | None = None, **env: str) -> object:
    return json.loads(_run(project, "print(json.dumps(checks.report()))", debug=debug, database=database, **env))


def test_django_says_its_checks_and_the_migrations_not_applied_or_not_made(project: Path) -> None:
    report = _report(project)

    assert report == {
        "checks": [W122],
        "migrations": {"unapplied": [{"app": "shop", "names": ["0001_initial"]}], "changes": CHANGES},
    }


def test_a_migration_applied_is_no_longer_said(project: Path) -> None:
    _run(project, "from django.core.management import call_command; call_command('migrate', verbosity=0)")

    report = _report(project)

    assert report == {"checks": [W122], "migrations": {"unapplied": [], "changes": CHANGES}}


def test_with_debug_off_the_deployment_checks_run_too(project: Path) -> None:
    report = _report(project, debug=False)

    assert report == {
        "checks": IsList(
            W122,
            IsPartialDict({"id": "security.W001", "level": "warning", "hint": None, "object": None}),
            IsPartialDict({"id": "security.W020", "message": "ALLOWED_HOSTS must not be empty in deployment."}),
            check_order=False,
            length=...,
        ),
        "migrations": {"unapplied": [{"app": "shop", "names": ["0001_initial"]}], "changes": CHANGES},
    }


def test_a_silenced_check_is_left_out(project: Path) -> None:
    report = _report(project, SILENCED='["fields.W122"]')

    assert report == IsPartialDict({"checks": []})


def test_a_database_that_doesnt_answer_leaves_the_migrations_applied_unknown(project: Path) -> None:
    pytest.importorskip("psycopg")
    # Nothing listens on port 1: the connection is refused at once.
    database = {"ENGINE": "django.db.backends.postgresql", "NAME": "shop", "HOST": "127.0.0.1", "PORT": "1"}

    report = _report(project, database=database)

    assert report == {"checks": [W122], "migrations": {"unapplied": None, "changes": CHANGES}}


def test_the_checks_thread_leaves_an_in_memory_database_alone(project: Path) -> None:
    # Its own connection to ":memory:" would be a new, empty database: every migration unapplied.
    database = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}

    report = json.loads(_run(project, "print(json.dumps(checks.in_own_thread()))", database=database))

    assert report == {"checks": [W122], "migrations": {"unapplied": None, "changes": CHANGES}}


@pytest.mark.django_db
def test_the_test_projects_migrations_one_marked_unapplied() -> None:
    MigrationRecorder(connection).record_unapplied("sessions", "0001_initial")

    report = checks.report()

    assert report == {
        "checks": [],
        "migrations": {"unapplied": [{"app": "sessions", "names": ["0001_initial"]}], "changes": []},
    }


def test_levels_by_their_lower_bound() -> None:
    assert [checks.level(number) for number in (10, 20, 25, 30, 40, 50, 60)] == [
        "debug",
        "info",
        "info",
        "warning",
        "error",
        "critical",
        "critical",
    ]
