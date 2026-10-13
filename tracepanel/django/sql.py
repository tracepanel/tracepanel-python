"""SQL under Django: an execute wrapper on every new database connection, recorded by tracepanel.core.sql.

The rows a query returned are counted as Django fetches them (its cursor's fetchone, fetchmany and fetchall, what
the ORM reads results with), into the statement's span: on every database, SQLite's included, whose driver doesn't
say how many rows a SELECT found.
"""

from collections.abc import Callable
from typing import Any

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.db.backends.utils import CursorWrapper

from tracepanel.core import sql
from tracepanel.core.model import Attrs, Span
from tracepanel.core.patching import patch
from tracepanel.django import services as django_services

# The span of the statement a Django cursor last ran: its fetches count into it.
_SPAN = "_tracepanel_span"

Context = dict[str, object]
Execute = Callable[[str, sql.Params, bool, Context], object]


def _database(connection: object) -> Attrs:
    """Where a statement ran: the DATABASES alias, Django's vendor, and the service behind it."""
    return {
        "db.client": "django",
        "db.alias": getattr(connection, "alias", None),
        "db.vendor": getattr(connection, "vendor", None),
        **django_services.for_connection(connection),
    }


def wrapper(execute: Execute, statement: str, params: sql.Params, many: bool, context: Context) -> object:  # noqa: FBT001 (Django's signature)
    return sql.run(
        lambda: execute(statement, params, many, context),
        statement,
        params,
        many=many,
        database=lambda: _database(context["connection"]),
        then=lambda span: setattr(context["cursor"], _SPAN, span),
    )


def _fetch(name: str) -> Callable[..., Any]:
    """The cursor's fetch, counting what it returned into the span of the statement it reads."""

    def fetch(self: CursorWrapper, *args: object, **kwargs: object) -> Any:  # noqa: ANN401 (rows, or a row)
        with self.db.wrap_database_errors:
            result = getattr(self.cursor, name)(*args, **kwargs)
        span: Span | None = getattr(self, _SPAN, None)
        if span is not None:
            sql.count_rows(span, (result is not None) if name == "fetchone" else len(result))
        return result

    return fetch


def add_wrapper(sender: object, connection: BaseDatabaseWrapper, **kwargs: object) -> None:  # noqa: ARG001 (signal API)
    if wrapper not in connection.execute_wrappers:
        connection.execute_wrappers.append(wrapper)


def install() -> None:
    # Connections are per thread, so a signal catches every one, including async ORM threads.
    connection_created.connect(add_wrapper, dispatch_uid="tracepanel.sql")
    # Django's cursor passes these on to the driver's (__getattr__): its own methods count first.
    for name in ("fetchone", "fetchmany", "fetchall"):
        patch(CursorWrapper, name, _fetch(name))
