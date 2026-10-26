from collections.abc import Generator
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from dirty_equals import IsInt, IsPartialDict, IsStr
from django.core.management import call_command
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.django import migrate

POSTGRES = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL's locks")
ID = IsStr(regex=r"[0-9A-Z]{26}")
OPERATIONS = {
    "0001_initial": ["CreateModel Product", "RunPython add_products"],
    "0002_product_sku": ["AddField Product.sku", "RunPython backfill_sku"],
    "0003_product_price": [
        "AlterField Product.name",
        "AddField Product.price",
        "AlterField Product.price",
        "AddField Product.note",
        "AlterField Product.note",
        "AddIndex Product.catalog_sku_idx",
    ],
}


@pytest.fixture
def catalog(settings: Settings) -> Generator[None, None, None]:
    """The catalog app installed, its tables not made: each test migrates it, and back to zero when it is done."""
    settings.INSTALLED_APPS = [*settings.INSTALLED_APPS, "tests.project.catalog"]
    yield
    call_command("migrate", "catalog", "zero", verbosity=0)


def operation_spans(trace: Trace, parent: Span) -> list[Span]:
    """The `migration.operation` spans under `parent`, the schema editor's transaction between them or not."""
    children = [span for span in trace.spans if span.parent_id == parent.id]
    found: list[Span] = []
    for span in children:
        if span.kind == "migration.operation":
            found.append(span)
        elif span.kind == "transaction":
            found += operation_spans(trace, span)
    return found


def migration_spans(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.attrs.get("migration.app") == "catalog"]


def operations(trace: Trace) -> dict[str, list[str]]:
    return {span.name: [each.name for each in operation_spans(trace, span)] for span in migration_spans(trace)}


def operation(trace: Trace, name: str) -> Span:
    return next(span for span in trace.spans if span.name == name)


@pytest.mark.django_db(transaction=True)
def test_migrate_has_a_span_per_migration_and_operation(
    catalog: None, traces: list[Trace], validator: Draft202012Validator
) -> None:
    del catalog
    call_command("migrate", "catalog", verbosity=0)
    (trace,) = traces
    validator.validate(trace.to_dict())
    root = trace.spans[0]
    first = migration_spans(trace)[0]
    backfill = operation(trace, "RunPython backfill_sku")
    by_id = {span.id: span for span in trace.spans}

    assert trace.attrs == IsPartialDict(
        {
            "command.name": "migrate",
            "migrate.plan": ["catalog.0001_initial", "catalog.0002_product_sku", "catalog.0003_product_price"],
            "migrate.planned": 3,
            "migrate.applied": 3,
            "migrate.unapplied": 0,
        }
    )

    assert first.to_dict() == {
        "id": ID,
        "parent_id": root.id,
        "kind": "migration",
        "name": "migrate catalog 0001_initial",
        "start": IsInt(),
        "duration": IsInt(ge=0),
        "status": "ok",
        "attrs": {
            "migration.app": "catalog",
            "migration.name": "0001_initial",
            "migration.direction": "forwards",
            "migration.atomic": True,
            "migration.fake": False,
            "migration.operations": 2,
        },
    }
    assert operations(trace) == {f"migrate catalog {name}": names for name, names in OPERATIONS.items()}

    # The code's UPDATE inside its operation, and the rows it changed.
    assert backfill.attrs == {
        "migration.operation": "RunPython",
        "migration.description": "Raw Python operation",
        "migration.atomic": True,
        "migration.rows": 3,
    }
    assert [span.name for span in trace.spans if span.parent_id == backfill.id] == ["UPDATE catalog_product"]

    # Every statement the schema editor ran for an operation is inside it.
    created = operation(trace, "CreateModel Product")
    assert [span.attrs["db.statement"] for span in trace.spans if span.parent_id == created.id] == [
        IsStr(regex=r'(?s)CREATE TABLE "catalog_product".*')
    ]
    assert by_id[str(operation(trace, "AddIndex Product.catalog_sku_idx").parent_id)].kind == "transaction"


@pytest.mark.django_db(transaction=True)
def test_migrating_back_unapplies_each_migration(catalog: None, traces: list[Trace]) -> None:
    del catalog
    call_command("migrate", "catalog", verbosity=0)
    call_command("migrate", "catalog", "0001", verbosity=0)
    trace = traces[1]

    assert trace.attrs == IsPartialDict(
        {
            "migrate.plan": ["catalog.0003_product_price", "catalog.0002_product_sku"],
            "migrate.planned": 2,
            "migrate.applied": 0,
            "migrate.unapplied": 2,
        }
    )
    assert operations(trace) == {
        "unapply catalog 0003_product_price": list(reversed(OPERATIONS["0003_product_price"])),
        "unapply catalog 0002_product_sku": list(reversed(OPERATIONS["0002_product_sku"])),
    }
    assert [span.attrs["migration.direction"] for span in migration_spans(trace)] == ["backwards", "backwards"]


@pytest.mark.django_db(transaction=True)
def test_a_faked_migration_runs_no_operations(catalog: None, traces: list[Trace]) -> None:
    del catalog
    call_command("migrate", "catalog", fake=True, verbosity=0)
    call_command("migrate", "catalog", "zero", fake=True, verbosity=0)
    applied, unapplied = traces

    assert [(span.name, span.attrs["migration.fake"]) for span in migration_spans(applied)] == [
        ("migrate catalog 0001_initial", True),
        ("migrate catalog 0002_product_sku", True),
        ("migrate catalog 0003_product_price", True),
    ]
    assert operations(applied) == {name: [] for name in operations(applied)}

    assert unapplied.attrs == IsPartialDict({"migrate.applied": 0, "migrate.unapplied": 3})


@pytest.mark.django_db(transaction=True)
def test_sqlmigrate_runs_nothing(catalog: None, traces: list[Trace]) -> None:
    del catalog
    call_command("sqlmigrate", "catalog", "0002")
    (trace,) = traces

    assert [span for span in trace.spans if span.kind.startswith("migration")] == []


def irreversible(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401 (RunPython's signature)
    del apps, schema_editor


@pytest.mark.django_db(transaction=True)
def test_operations_without_a_reverse_are_marked() -> None:
    class Backfill(migrations.Migration):
        operations: ClassVar[list[Operation]] = [
            migrations.RunPython(irreversible),
            migrations.RunSQL("SELECT 1"),
            migrations.SeparateDatabaseAndState(database_operations=[migrations.RunSQL("SELECT 2", "SELECT 3")]),
        ]

    migration = Backfill("0001_backfill", "ledger")
    executor = MigrationExecutor(connection)
    try:
        with start_trace("command", "manage.py migrate") as trace:
            executor.apply_migration(ProjectState(), migration)
    finally:
        executor.recorder.record_unapplied("ledger", "0001_backfill")
    (span,) = [span for span in trace.spans if span.name == "migrate ledger 0001_backfill"]
    names = {span.id: span.name for span in trace.spans}

    assert trace.attrs == IsPartialDict({"migrate.applied": 1})

    assert [(each.name, each.attrs.get("migration.risk")) for each in operation_spans(trace, span)] == [
        ("RunPython irreversible", "RunPython with no reverse: the migration can't be unapplied"),
        ("RunSQL", "RunSQL with no reverse: the migration can't be unapplied"),
        ("SeparateDatabaseAndState", None),
    ]
    # The operation a SeparateDatabaseAndState runs on the database is inside it.
    assert [(each.name, names[str(each.parent_id)]) for each in trace.spans if each.name == "RunSQL"] == [
        ("RunSQL", IsStr(regex="(transaction default|migrate ledger 0001_backfill)")),
        ("RunSQL", "SeparateDatabaseAndState"),
    ]


@POSTGRES
@pytest.mark.django_db(transaction=True)
def test_operations_that_lock_a_postgresql_table_are_marked(catalog: None, traces: list[Trace]) -> None:
    del catalog
    call_command("migrate", "catalog", verbosity=0)
    call_command("migrate", "catalog", "0002", verbosity=0)
    applied, unapplied = traces

    def risks(trace: Trace) -> list[tuple[str, object]]:
        (migration,) = (span for span in migration_spans(trace) if span.attrs["migration.name"] == "0003_product_price")
        return [(span.name, span.attrs.get("migration.risk")) for span in operation_spans(trace, migration)]

    assert risks(applied) == [
        # varchar(50) to varchar(100): binary coercible, no rewrite.
        ("AlterField Product.name", None),
        ("AddField Product.price", None),
        # integer to bigint, and NOT NULL.
        ("AlterField Product.price", migrate.REWRITES),
        ("AddField Product.note", None),
        ("AlterField Product.note", migrate.SCANS),
        ("AddIndex Product.catalog_sku_idx", migrate.BLOCKS_WRITES),
    ]

    assert risks(unapplied) == [
        ("AddIndex Product.catalog_sku_idx", None),
        ("AlterField Product.note", None),
        ("AddField Product.note", None),
        ("AlterField Product.price", migrate.REWRITES),
        ("AddField Product.price", None),
        # Shorter: each value is checked.
        ("AlterField Product.name", migrate.REWRITES),
    ]


class AddIndexConcurrently(migrations.AddIndex):
    """Named like django.contrib.postgres's, which needs psycopg: the name is what tells."""


class RemoveIndexConcurrently(migrations.RemoveIndex):
    """Named like django.contrib.postgres's."""


@pytest.mark.parametrize(
    ("operation", "backwards", "expected"),
    [
        (migrations.AddIndex("product", models.Index(fields=["sku"], name="sku_idx")), False, migrate.BLOCKS_WRITES),
        (migrations.AddIndex("product", models.Index(fields=["sku"], name="sku_idx")), True, None),
        (migrations.RemoveIndex("product", "sku_idx"), False, None),
        # Unapplied, it creates the index.
        (migrations.RemoveIndex("product", "sku_idx"), True, migrate.BLOCKS_WRITES),
        (AddIndexConcurrently("product", models.Index(fields=["sku"], name="sku_idx")), False, None),
        (RemoveIndexConcurrently("product", "sku_idx"), True, None),
        # Unapplying it fails before it runs: nothing to hint.
        (migrations.RunPython(irreversible), True, None),
    ],
)
def test_index_risk_by_direction(operation: Operation, *, backwards: bool, expected: str | None) -> None:
    editor = SimpleNamespace(connection=SimpleNamespace(vendor="postgresql"))
    states = (ProjectState(), ProjectState())

    assert migrate.risk(operation, "catalog", editor, states, backwards=backwards) == expected


@pytest.mark.parametrize(
    ("vendor", "old", "new", "expected"),
    [
        ("postgresql", "varchar(50)", "varchar(100)", None),
        ("postgresql", "varchar(100)", "varchar(50)", migrate.REWRITES),
        ("postgresql", "varchar(50)", "text", None),
        ("postgresql", "text", "varchar(50)", migrate.REWRITES),
        ("postgresql", "numeric(10, 2)", "numeric(12, 2)", None),
        ("postgresql", "numeric(10, 2)", "numeric(12, 3)", migrate.REWRITES),
        ("postgresql", "integer", "bigint", migrate.REWRITES),
        ("postgresql", "integer", "integer", None),
        ("mysql", "varchar(50)", "varchar(100)", None),
        ("mysql", "varchar(100)", "varchar(50)", migrate.COPIES),
        ("mysql", "integer", "bigint", migrate.COPIES),
        ("mariadb", "integer", "bigint", None),
        ("sqlite", "integer", "bigint", None),
    ],
)
def test_type_change_risk(vendor: str, old: str, new: str, expected: str | None) -> None:
    assert migrate.type_change_risk(vendor, old, new) == expected
