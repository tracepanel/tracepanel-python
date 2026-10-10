"""EXPLAIN for a captured query: the statement for each database, and its plan read back.

The framework runs the statement on the database the query came from (Django: tracepanel.django.explain).

Only statements captured with their parameters (see `capture_sql_params`) and starting with SELECT are explained.
`analyze` (PostgreSQL, MySQL) executes the query to measure it, which is why writes are never accepted.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from tracepanel.core.model import Scalar

_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_SQLITE_SCAN = re.compile(r"^SCAN (?:TABLE )?(\w+)(?!.*\bUSING\b)")
_PG_SEQ_SCAN = re.compile(r"Seq Scan on (\w+)")


class ExplainError(ValueError):
    """A query that can't be explained, with a message for the user."""


@dataclass(slots=True)
class PlanLine:
    depth: int
    text: str
    warning: str | None = None


@dataclass(slots=True)
class Plan:
    vendor: str
    alias: str
    analyzed: bool
    lines: list[PlanLine] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "alias": self.alias,
            "analyzed": self.analyzed,
            "lines": [{"depth": line.depth, "text": line.text, "warning": line.warning} for line in self.lines],
        }


def _sqlite_warning(text: str) -> str | None:
    if match := _SQLITE_SCAN.match(text):
        return f"Full scan of {match.group(1)}: no index is used"
    if "USE TEMP B-TREE" in text:
        return "Sorts or groups in a temporary b-tree: no index matches ORDER BY / GROUP BY"
    return None


def _postgres_warning(text: str) -> str | None:
    if match := _PG_SEQ_SCAN.search(text):
        return f"Sequential scan of {match.group(1)}: no index is used"
    if "Sort Method: external" in text:
        return "Sort spills to disk"
    return None


def _sqlite(rows: list[tuple[object, ...]]) -> list[PlanLine]:
    # Rows are (id, parent, notused, detail); depth follows the parent chain.
    depth: dict[int, int] = {0: -1}
    lines: list[PlanLine] = []
    for node, parent, _, detail in rows:
        level = depth.get(int(str(parent)), -1) + 1
        depth[int(str(node))] = level
        lines.append(PlanLine(level, str(detail), _sqlite_warning(str(detail))))
    return lines


def _postgres(rows: list[tuple[object, ...]]) -> list[PlanLine]:
    lines: list[PlanLine] = []
    for (row,) in rows:
        text = str(row)
        indent = len(text) - len(text.lstrip())
        body = text.strip().removeprefix("->").strip()
        # Nodes are "->" lines indented 6 per level; their detail lines (Filter:, Buffers:) sit 2 deeper.
        depth = (indent + 2) // 6 if text.strip().startswith("->") else (indent + 4) // 6
        lines.append(PlanLine(depth, body, _postgres_warning(body)))
    return lines


def _table(columns: list[str], rows: list[tuple[object, ...]]) -> list[PlanLine]:
    """MySQL's tabular EXPLAIN: one line per row, as `column=value` pairs."""
    lines = []
    for row in rows:
        pairs = [f"{name}={value}" for name, value in zip(columns, row, strict=False) if value is not None]
        lines.append(PlanLine(0, "  ".join(pairs)))
    return lines


def _scalar(value: object) -> Scalar:
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def params_from(attrs: Mapping[str, object]) -> list[Scalar] | dict[str, Scalar] | None:
    """Parameters stored on a span by `sql.params_attrs`, back in the shape the cursor takes."""
    values = attrs.get("db.params")
    if not isinstance(values, list):
        return None
    scalars = [_scalar(value) for value in values]
    names = attrs.get("db.param_names")
    return dict(zip((str(name) for name in names), scalars, strict=True)) if isinstance(names, list) else scalars


Rows = list[tuple[object, ...]]
# Runs a statement on the query's database and returns (rows, column names); raises ExplainError if it fails.
Execute = Callable[[str, Sequence[Scalar] | Mapping[str, Scalar] | None], tuple[Rows, list[str]]]


def explain(  # noqa: PLR0913 (the statement, where it ran, how to run it)
    statement: str,
    params: Sequence[Scalar] | Mapping[str, Scalar] | None,
    *,
    vendor: str,
    alias: str,
    execute: Execute,
    analyze: bool = False,
) -> Plan:
    if not _SELECT.match(statement):
        msg = "Only SELECT statements can be explained."
        raise ExplainError(msg)
    if params is None:
        msg = "Parameters of this query were not captured (capture_sql_params is off, or a value was too large)."
        raise ExplainError(msg)
    if vendor == "sqlite":
        prefix, analyze = "EXPLAIN QUERY PLAN ", False
    elif vendor == "postgresql":
        prefix = "EXPLAIN (ANALYZE, BUFFERS) " if analyze else "EXPLAIN "
    elif vendor == "mysql":
        prefix = "EXPLAIN ANALYZE " if analyze else "EXPLAIN "
    else:
        prefix, analyze = "EXPLAIN ", False
    rows, columns = execute(prefix + statement, params)
    if vendor == "sqlite":
        lines = _sqlite(rows)
    elif vendor == "postgresql" or (vendor == "mysql" and analyze):
        lines = _postgres(rows)
    else:
        lines = _table(columns, rows)
    return Plan(vendor=vendor, alias=alias, analyzed=analyze, lines=lines)
