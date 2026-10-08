"""SQL under Django: an execute wrapper on every new database connection, recorded by tracepanel.core.sql."""

from collections.abc import Callable

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.signals import connection_created

from tracepanel.core import sql
from tracepanel.core.model import Attrs
from tracepanel.django import services as django_services

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
    )


def add_wrapper(sender: object, connection: BaseDatabaseWrapper, **kwargs: object) -> None:  # noqa: ARG001 (signal API)
    if wrapper not in connection.execute_wrappers:
        connection.execute_wrappers.append(wrapper)


def install() -> None:
    # Connections are per thread, so a signal catches every one, including async ORM threads.
    connection_created.connect(add_wrapper, dispatch_uid="tracepanel.sql")
