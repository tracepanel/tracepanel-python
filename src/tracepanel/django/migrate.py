"""migrate, what it did: a span per migration applied or unapplied, a span per operation in it, its SQL inside.

`manage.py migrate` is a `command` trace (tracepanel.django.commands). Under it, each migration the plan runs is a
span, `migrate shop 0012_add_sku` (`unapply shop 0012_add_sku` going backwards), with whether it ran in a
transaction (`migration.atomic`: the migration's atomic and a database that can roll DDL back) and whether it was
faked. Each of its operations is a span inside, `AddField Product.sku`, `RunPython backfill_sku`, holding the
statements the schema editor ran for it, or the queries its code made; the rows its INSERT, UPDATE and DELETE
statements changed (`migration.rows`) when it ran any. SQL the schema editor defers to the migration's end (indexes,
foreign keys) is the migration's own. The trace says what the plan was and how many migrations were applied and
unapplied.

An operation known to lock or rewrite a table on the database it runs on says so, `migration.risk`, a short reason:
a hint for the migration's review, from the database's own documentation, not a measurement.

- PostgreSQL: a column's type changed (ALTER COLUMN TYPE rewrites the table under an ACCESS EXCLUSIVE lock, unless
  the old type is binary coercible to the new one: a longer or unlimited varchar, varchar to text, a numeric with
  more precision and the same scale); a column made NOT NULL (the table is scanned under that lock); AddIndex (CREATE
  INDEX blocks writes until it is built; AddIndexConcurrently doesn't).
- MySQL: a column's type changed (a table copy, ALGORITHM=COPY, writes wait), a longer varchar aside, which can be
  done in place.
- Any database: RunPython or RunSQL without its reverse, which leaves the migration impossible to unapply.

Adding a column is no rewrite on the versions Django supports: PostgreSQL 14 stores a constant default in the
catalog, MySQL 8 adds the column in place.
"""

import contextlib
import functools
import re
from collections.abc import Callable, Generator, Sequence
from contextvars import ContextVar
from typing import Any

from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.migration import Migration
from django.db.migrations.operations import AddIndex, AlterField, RunPython, RunSQL, SeparateDatabaseAndState
from django.db.migrations.operations.base import Operation
from django.db.migrations.operations.fields import FieldOperation
from django.db.migrations.operations.models import ModelOperation
from django.db.migrations.state import ProjectState

from tracepanel.core.context import current_trace, start_span
from tracepanel.core.patching import patch, patched

# The plan's migrations the trace names; a longer plan is counted (`migrate.planned`).
MAX_PLAN = 100

REWRITES = "ALTER COLUMN TYPE rewrites the table under an ACCESS EXCLUSIVE lock"
SCANS = "SET NOT NULL scans the table under an ACCESS EXCLUSIVE lock"
BLOCKS_WRITES = "CREATE INDEX blocks writes to the table until it is built (AddIndexConcurrently doesn't)"
COPIES = "changing a column's type copies the table (ALGORITHM=COPY): writes wait"
IRREVERSIBLE = "no reverse: the migration can't be unapplied"

# The operation whose span is open: an operation run from its own other direction (AlterField's backwards runs its
# forwards) is that span's.
_operation: ContextVar[Operation | None] = ContextVar("tracepanel_migration_operation", default=None)

_TYPE = re.compile(r"(\w+)(?:\((\d+)(?:,\s*(\d+))?\))?")
_WRITES = ("INSERT", "UPDATE", "DELETE")


def _parsed(db_type: str) -> tuple[str, int | None, int | None]:
    """`varchar(20)` as ("varchar", 20, None), `numeric(10, 2)` as ("numeric", 10, 2)."""
    found = _TYPE.fullmatch(db_type.strip().lower())
    if found is None:
        return db_type.strip().lower(), None, None
    name, size, scale = found.groups()
    return name, int(size) if size else None, int(scale) if scale else None


def _binary_coercible(old: str, new: str) -> bool:
    """Whether PostgreSQL changes the type without rewriting the table (its 9.2 release notes, ALTER TABLE's docs)."""
    (old_name, old_size, old_scale), (new_name, new_size, new_scale) = _parsed(old), _parsed(new)
    if old_name == "varchar" and new_name == "text":
        return True
    if old_name == new_name == "varchar":
        return new_size is None or (old_size is not None and new_size >= old_size)
    if old_name == new_name == "numeric":
        return new_size is None or (old_size is not None and new_size >= old_size and new_scale == old_scale)
    return False


def type_change_risk(vendor: str, old: str, new: str) -> str | None:
    """What changing a column's type from `old` to `new` (Django's db_type) costs on the vendor, when it locks."""
    if old.strip().lower() == new.strip().lower():
        return None
    if vendor == "postgresql":
        return None if _binary_coercible(old, new) else REWRITES
    if vendor == "mysql":
        (old_name, old_size, _), (new_name, new_size, _) = _parsed(old), _parsed(new)
        # A longer varchar is done in place when its length bytes stay as many, which the character set decides: no
        # hint either way.
        if old_name == new_name == "varchar" and old_size is not None and new_size is not None and new_size > old_size:
            return None
        return COPIES
    return None


def _field(state: ProjectState, app_label: str, model_name: str, name: str) -> Any:  # noqa: ANN401 (a Field)
    return state.apps.get_model(app_label, model_name)._meta.get_field(name)  # noqa: SLF001 (Django's Options API)


def _vendor(connection: Any) -> str:  # noqa: ANN401 (a DatabaseWrapper)
    """Django's vendor, MariaDB apart from MySQL: it changes some column types in place, MySQL's rules aren't its."""
    if connection.vendor == "mysql" and getattr(connection, "mysql_is_mariadb", False):
        return "mariadb"
    return str(connection.vendor)


def _alter_field_risk(
    operation: AlterField,
    app_label: str,
    connection: Any,  # noqa: ANN401 (a DatabaseWrapper)
    states: tuple[ProjectState, ProjectState],
) -> str | None:
    from_state, to_state = states
    old = _field(from_state, app_label, operation.model_name, operation.name)
    new = _field(to_state, app_label, operation.model_name, operation.name)
    old_type, new_type = old.db_type(connection), new.db_type(connection)
    vendor = _vendor(connection)
    changed = type_change_risk(vendor, old_type, new_type) if old_type and new_type else None
    if changed is not None:
        return changed
    if vendor == "postgresql" and old.null and not new.null:
        return SCANS
    return None


def risk(
    operation: Operation,
    app_label: str,
    schema_editor: Any,  # noqa: ANN401 (a BaseDatabaseSchemaEditor)
    states: tuple[ProjectState, ProjectState],
    *,
    backwards: bool,
) -> str | None:
    """Why the operation locks or rewrites a table on the schema editor's database, or can't be undone; None if not.

    `states`: what the database has before the operation, and what it will have after.
    """
    if isinstance(operation, AlterField):
        return _alter_field_risk(operation, app_label, schema_editor.connection, states)
    if backwards:
        return None
    if isinstance(operation, RunPython | RunSQL) and not operation.reversible:
        return f"{type(operation).__name__} with {IRREVERSIBLE}"
    concurrently = any(cls.__name__ == "AddIndexConcurrently" for cls in type(operation).__mro__)
    if isinstance(operation, AddIndex) and not concurrently and schema_editor.connection.vendor == "postgresql":
        return BLOCKS_WRITES
    return None


def _model(states: tuple[ProjectState, ...], app_label: str, name: str) -> str:
    """The model as its class is named (`Product`), where an operation has it lowercased (`product`)."""
    for state in states:
        model = state.models.get((app_label, name.lower()))
        if model is not None:
            return str(model.name)
    return name


def subject(operation: Operation, app_label: str, states: tuple[ProjectState, ...]) -> str | None:
    """What the operation works on: `Product.sku` for a field, `Product` for a model, the code a RunPython runs."""
    if isinstance(operation, RunPython):
        return getattr(operation.code, "__name__", None)
    if isinstance(operation, FieldOperation):
        return f"{_model(states, app_label, operation.model_name)}.{operation.name}"
    if isinstance(operation, ModelOperation):
        return _model(states, app_label, operation.name)
    model_name = getattr(operation, "model_name", None)
    if not isinstance(model_name, str):
        return None
    item = getattr(operation, "index", None) or getattr(operation, "constraint", None)
    part = getattr(item, "name", None) or getattr(operation, "new_name", None) or getattr(operation, "name", None)
    model = _model(states, app_label, model_name)
    return f"{model}.{part}" if part else model


def _rowcount(cursor: object) -> int | None:
    count = getattr(cursor, "rowcount", -1)
    return count if isinstance(count, int) and count >= 0 else None


class _Rows:
    """The rows the INSERT, UPDATE and DELETE statements changed, from their cursors' rowcount: an execute wrapper.

    A cursor's is read right after the statement, and again once it runs another one or the operation is over: the
    larger counts. psycopg forgets it when Django closes the cursor, SQLite counts the rows of a statement with
    RETURNING (bulk_create's) as they are fetched.
    """

    def __init__(self) -> None:
        self.known: int | None = None
        self.pending: list[tuple[object, int | None]] = []

    def _settle(self, cursor: object, first: int | None) -> None:
        counts = [count for count in (first, _rowcount(cursor)) if count is not None]
        if counts:
            self.known = (self.known or 0) + max(counts)

    def __call__(
        self,
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,  # noqa: FBT001 (Django's signature)
        context: dict[str, Any],
    ) -> object:
        cursor = context.get("cursor")
        for pending, first in [entry for entry in self.pending if entry[0] is cursor]:
            self._settle(pending, first)
        self.pending = [entry for entry in self.pending if entry[0] is not cursor]
        result = execute(sql, params, many, context)
        if str(sql).lstrip()[:6].upper() in _WRITES:
            self.pending.append((cursor, _rowcount(cursor)))
        return result

    def count(self) -> int | None:
        """How many rows, None when no statement changed any or the driver doesn't say (a rowcount of -1)."""
        for cursor, first in self.pending:
            self._settle(cursor, first)
        self.pending = []
        return self.known


def _describe(operation: Operation) -> str | None:
    try:
        return str(operation.describe())
    except Exception:  # noqa: BLE001 (a description is no reason to fail a migration)
        return None


def _traced(operation: Operation, method: str, migration: Migration, *, atomic: bool) -> Callable[..., object]:
    original = getattr(operation, method)
    backwards = method == "database_backwards"

    @functools.wraps(original)
    def run(app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState) -> object:  # noqa: ANN401
        if _operation.get() is operation:
            return original(app_label, schema_editor, from_state, to_state)
        states = (from_state, to_state)
        name = type(operation).__name__
        target = subject(operation, app_label, states)
        attrs: dict[str, Any] = {
            "migration.operation": name,
            "migration.description": _describe(operation),
            "migration.atomic": atomic
            or bool(operation.atomic or (migration.atomic and operation.atomic is not False)),
        }
        with contextlib.suppress(Exception):  # a hint that can't be worked out is left out
            found = risk(operation, app_label, schema_editor, states, backwards=backwards)
            if found is not None:
                attrs["migration.risk"] = found
        rows = _Rows()
        token = _operation.set(operation)
        try:
            with start_span("function", f"{name} {target}" if target else name, attrs) as span:
                with schema_editor.connection.execute_wrapper(rows):
                    result = original(app_label, schema_editor, from_state, to_state)
                count = rows.count() if span is not None else None
                if span is not None and count is not None:
                    span.attrs["migration.rows"] = count
                return result
        finally:
            _operation.reset(token)

    return run


def _operations(operations: Sequence[Operation]) -> Generator[Operation, None, None]:
    """The operations, and those a SeparateDatabaseAndState runs on the database, inside it."""
    for operation in operations:
        yield operation
        if isinstance(operation, SeparateDatabaseAndState):
            yield from _operations(operation.database_operations)


@contextlib.contextmanager
def _operation_spans(migration: Migration, schema_editor: Any) -> Generator[None, None, None]:  # noqa: ANN401
    """Each operation's database_forwards and database_backwards as a span, on the operation, while the block runs."""
    wrapped = []
    for operation in _operations(migration.operations):
        for method in ("database_forwards", "database_backwards"):
            try:
                vars(operation)[method] = _traced(operation, method, migration, atomic=schema_editor.atomic_migration)
            except TypeError:
                # No __dict__ to put it in: the operation runs without its span.
                continue
            wrapped.append((operation, method))
    try:
        yield
    finally:
        for operation, method in wrapped:
            vars(operation).pop(method, None)


def _migration_span(migration: Migration, executor: MigrationExecutor, *, backwards: bool, fake: bool) -> Any:  # noqa: ANN401 (a context manager)
    verb = "unapply" if backwards else "migrate"
    connection = executor.connection
    return start_span(
        "function",
        f"{verb} {migration.app_label} {migration.name}",
        {
            "migration.app": migration.app_label,
            "migration.name": migration.name,
            "migration.direction": "backwards" if backwards else "forwards",
            "migration.atomic": bool(migration.atomic and connection.features.can_rollback_ddl),
            "migration.fake": fake,
            "migration.operations": len(migration.operations),
        },
    )


# Whether the migration's operations ran (apply() or unapply()): one that didn't was faked.
_ran: ContextVar[list[bool] | None] = ContextVar("tracepanel_migration_ran", default=None)


def _count(key: str) -> None:
    trace = current_trace()
    if trace is not None:
        known = trace.attrs.get(key)
        trace.attrs[key] = (known if isinstance(known, int) else 0) + 1


def _run_migration(original: Callable[..., Any], *, backwards: bool) -> Callable[..., Any]:
    @functools.wraps(original)
    def run(self: MigrationExecutor, state: ProjectState, migration: Migration, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(self, state, migration, *args, **kwargs)
        ran: list[bool] = []
        token = _ran.set(ran)
        try:
            fake = bool(kwargs.get("fake", args[0] if args else False))
            with _migration_span(migration, self, backwards=backwards, fake=fake) as span:
                result = original(self, state, migration, *args, **kwargs)
                if span is not None:
                    # --fake-initial fakes a migration whose tables are there already.
                    span.attrs["migration.fake"] = not ran
        finally:
            _ran.reset(token)
        _count("migrate.unapplied" if backwards else "migrate.applied")
        return result

    return run


def _run_operations(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def run(self: Migration, project_state: ProjectState, schema_editor: Any, collect_sql: bool = False) -> Any:  # noqa: ANN401, FBT001, FBT002 (Django's signature)
        ran = _ran.get()
        # sqlmigrate collects the SQL without running it: nothing to trace.
        if collect_sql or ran is None or current_trace() is None:
            return original(self, project_state, schema_editor, collect_sql)
        ran.append(True)
        with _operation_spans(self, schema_editor):
            return original(self, project_state, schema_editor, collect_sql)

    return run


def _plan_attrs(plan: list[tuple[Migration, bool]]) -> dict[str, Any]:
    return {
        "migrate.plan": [f"{migration.app_label}.{migration.name}" for migration, _ in plan[:MAX_PLAN]],
        "migrate.planned": len(plan),
        "migrate.applied": 0,
        "migrate.unapplied": 0,
    }


def install() -> None:
    if patched(MigrationExecutor, "migrate"):
        return
    migrate = MigrationExecutor.migrate

    @functools.wraps(migrate)
    def traced_migrate(self: MigrationExecutor, targets: Any, plan: Any = None, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        trace = current_trace()
        if trace is not None:
            # What migrate() works out itself when not given it.
            plan = self.migration_plan(targets) if plan is None else plan
            trace.attrs.update(_plan_attrs(plan))
        return migrate(self, targets, plan, *args, **kwargs)

    patch(MigrationExecutor, "migrate", traced_migrate)
    patch(MigrationExecutor, "apply_migration", _run_migration(MigrationExecutor.apply_migration, backwards=False))
    patch(MigrationExecutor, "unapply_migration", _run_migration(MigrationExecutor.unapply_migration, backwards=True))
    patch(Migration, "apply", _run_operations(Migration.apply))
    patch(Migration, "unapply", _run_operations(Migration.unapply))
