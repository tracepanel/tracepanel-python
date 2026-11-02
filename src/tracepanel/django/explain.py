"""EXPLAIN under Django: the plan is built and read by tracepanel.core.explain, run on the query's DATABASES alias.

EXPLAIN ANALYZE runs the query: in an atomic block that is always rolled back, a savepoint when the connection is
already in a transaction (an ATOMIC_REQUESTS view's), so the app's own transaction is left as it was.
"""

from collections.abc import Mapping, Sequence

from django.db import DatabaseError, connections, transaction

from tracepanel.core import explain as plans
from tracepanel.core.model import Scalar


def explain(
    statement: str, params: Sequence[Scalar] | Mapping[str, Scalar] | None, alias: str, *, analyze: bool = False
) -> plans.Plan:
    if alias not in connections:
        msg = f"Unknown database alias {alias!r}."
        raise plans.ExplainError(msg)
    connection = connections[alias]

    def execute(sql: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
        try:
            if session in plans.ROLLBACK_SESSIONS.values():
                with transaction.atomic(using=alias), connection.cursor() as cursor:
                    try:
                        return plans.cursor_rows(cursor, sql, values)
                    finally:
                        # Marked once it ran: Django runs nothing more in a block marked for rollback.
                        transaction.set_rollback(True, using=alias)
            with connection.cursor() as cursor:
                return plans.cursor_rows(cursor, sql, values, session)
        except DatabaseError as exc:
            msg = f"The database could not explain this query: {exc}"
            raise plans.ExplainError(msg) from exc

    return plans.explain(statement, params, vendor=connection.vendor, alias=alias, execute=execute, analyze=analyze)
