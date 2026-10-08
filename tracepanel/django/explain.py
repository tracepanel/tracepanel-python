"""EXPLAIN under Django: the plan is built and read by tracepanel.core.explain, run on the query's DATABASES alias."""

from collections.abc import Mapping, Sequence

from django.db import DatabaseError, connections

from tracepanel.core import explain as plans
from tracepanel.core.model import Scalar


def explain(
    statement: str, params: Sequence[Scalar] | Mapping[str, Scalar] | None, alias: str, *, analyze: bool = False
) -> plans.Plan:
    if alias not in connections:
        msg = f"Unknown database alias {alias!r}."
        raise plans.ExplainError(msg)
    connection = connections[alias]

    def execute(sql: str, values: Sequence[Scalar] | Mapping[str, Scalar] | None) -> tuple[plans.Rows, list[str]]:
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, values)
                rows = [tuple(row) for row in cursor.fetchall()]
                return rows, [str(column[0]) for column in cursor.description or []]
        except DatabaseError as exc:
            msg = f"The database could not explain this query: {exc}"
            raise plans.ExplainError(msg) from exc

    return plans.explain(statement, params, vendor=connection.vendor, alias=alias, execute=execute, analyze=analyze)
