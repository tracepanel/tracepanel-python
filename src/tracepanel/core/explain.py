"""EXPLAIN for a captured query: the statement for each database, and its plan read back.

The framework runs the statement on the database the query came from (Django: tracepanel.django.explain).

Only statements captured with their parameters (see `capture_sql_params`) and starting with SELECT are explained.
`analyze` (PostgreSQL, MySQL) executes the query to measure it, which is why writes are never accepted.

SQL Server has no EXPLAIN: its session is set to show plans (SET SHOWPLAN_ALL ON) for the statement, on the same
connection, then set back. Its plan is estimated only, and its parameters are written into the statement as literals:
a statement sent with parameters (pyodbc's) shows no plan. The server compiles it without running it.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tracepanel.core.model import Scalar
from tracepanel.core.services import system

_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
# What a placeholder can't be inside: quoted strings and names, and comments.
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|--[^\n]*|/\*.*?\*/", re.DOTALL)
# %s, %(name)s, ?, ?1, :name (not a :: cast), $1.
_PLACEHOLDER = re.compile(r"%s|%\(\w+\)s|\?|(?<!:):(?!:)[A-Za-z_]\w*|\$\d+")
_SQLITE_SCAN = re.compile(r"^SCAN (?:TABLE )?(\w+)(?!.*\bUSING\b)")
_PG_SEQ_SCAN = re.compile(r"Seq Scan on (\w+)")
# An operator of SHOWPLAN_ALL as it reads: "   |    |--Clustered Index Scan(OBJECT:([shop].[dbo].[orders].[PK])".
_MSSQL_TREE = re.compile(r"^[\s|]*--")
_MSSQL_SCAN = re.compile(r"^(?:Table Scan|Clustered Index Scan)\(OBJECT:\(([^)]*)\)")
_MSSQL_NAME = re.compile(r"\[([^\]]+)\]")
# What a SQL Server statement's parameters are written in place of: %s and %(name)s (with %% for %), or ?.
_MSSQL_PARAM = re.compile(r"%%|%s|%\((\w+)\)s|\?")
# The statements around a SQL Server EXPLAIN, on its connection.
MSSQL_SESSION = ("SET SHOWPLAN_ALL ON", "SET SHOWPLAN_ALL OFF")


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


def _mssql_literal(value: Scalar) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    return "N'" + value.replace("'", "''") + "'"


def _mssql_inline(statement: str, params: Sequence[Scalar] | Mapping[str, Scalar] | None) -> str:
    """The statement with its parameters written in as SQL Server literals (quoted strings and comments kept)."""
    if params is None:
        return statement
    positional = iter(params) if not isinstance(params, Mapping) else iter(())
    # %% is a % only where the driver formats the statement with %s or %(name)s.
    formats = any(match.group() != "?" for match in _MSSQL_PARAM.finditer(_QUOTED.sub("", statement)))

    def value(match: re.Match[str]) -> str:
        if match.group() == "%%":
            return "%" if formats else "%%"
        try:
            if match.group(1) is not None:
                return _mssql_literal(params[match.group(1)])  # type: ignore[call-overload]
            return _mssql_literal(next(positional))
        except (KeyError, StopIteration, TypeError) as exc:
            msg = "The captured parameters don't match the query's placeholders."
            raise ExplainError(msg) from exc

    parts: list[str] = []
    end = 0
    for quoted in _QUOTED.finditer(statement):
        parts.append(_MSSQL_PARAM.sub(value, statement[end : quoted.start()]))
        parts.append(quoted.group().replace("%%", "%") if formats else quoted.group())
        end = quoted.end()
    parts.append(_MSSQL_PARAM.sub(value, statement[end:]))
    return "".join(parts)


def _mssql_warning(text: str, warnings: object) -> str | None:
    if match := _MSSQL_SCAN.match(text):
        # [database].[schema].[table], and the clustered index's name after it.
        names = _MSSQL_NAME.findall(match.group(1).partition(" AS ")[0])
        table = names[2] if len(names) > 2 else names[-1] if names else "a table"  # noqa: PLR2004
        return f"Full scan of {table}: no index is used"
    return f"SQL Server warns: {warnings}" if warnings else None


def _mssql(columns: list[str], rows: list[tuple[object, ...]]) -> list[PlanLine]:
    """SHOWPLAN_ALL: the statement's own row, then a PLAN_ROW per operator with its parent's NodeId."""
    depth: dict[int, int] = {}
    lines: list[PlanLine] = []
    for row in rows:
        values = dict(zip(columns, row, strict=False))
        if values.get("Type") != "PLAN_ROW":
            continue
        level = depth.get(int(str(values["Parent"])), -1) + 1
        depth[int(str(values["NodeId"]))] = level
        text = _MSSQL_TREE.sub("", str(values["StmtText"]), count=1).strip()
        estimate = f"rows={float(str(values['EstimateRows'])):g} cost={float(str(values['TotalSubtreeCost'])):.4g}"
        lines.append(PlanLine(level, f"{text}  ({estimate})", _mssql_warning(text, values.get("Warnings"))))
    return lines


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
Values = Sequence[Scalar] | Mapping[str, Scalar] | None
# Statements to run before and after it on the same connection, the second whatever happens (SQL Server's SHOWPLAN).
Session = tuple[str, str] | None
# Runs a statement on the query's database and returns (rows, column names); raises ExplainError if it fails.
Execute = Callable[[str, Values, Session], tuple[Rows, list[str]]]


def cursor_rows(cursor: Any, sql_text: str, values: object, session: Session = None) -> tuple[Rows, list[str]]:  # noqa: ANN401 (a DB-API cursor)
    """Run a statement on a DB-API cursor, inside the session when there is one: its rows and column names."""
    if session is not None:
        cursor.execute(session[0])
    try:
        if values is None:
            cursor.execute(sql_text)
        else:
            cursor.execute(sql_text, values if isinstance(values, Mapping) else tuple(values))  # type: ignore[arg-type]
        rows = [tuple(row) for row in cursor.fetchall()]
        return rows, [str(column[0]) for column in cursor.description or []]
    finally:
        if session is not None:
            cursor.execute(session[1])


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
    if params is None and _PLACEHOLDER.search(_QUOTED.sub("", statement)):
        msg = "Parameters of this query were not captured (capture_sql_params is off, or a value was too large)."
        raise ExplainError(msg)
    session: Session = None
    if system(vendor) == "mssql":
        prefix, analyze, session = "", False, MSSQL_SESSION
        statement, params = _mssql_inline(statement, params), None
    elif vendor == "sqlite":
        prefix, analyze = "EXPLAIN QUERY PLAN ", False
    elif vendor == "postgresql":
        prefix = "EXPLAIN (ANALYZE, BUFFERS) " if analyze else "EXPLAIN "
    elif vendor == "mysql":
        prefix = "EXPLAIN ANALYZE " if analyze else "EXPLAIN "
    else:
        prefix, analyze = "EXPLAIN ", False
    rows, columns = execute(prefix + statement, params, session)
    if session is not None:
        lines = _mssql(columns, rows)
    elif vendor == "sqlite":
        lines = _sqlite(rows)
    elif vendor == "postgresql" or (vendor == "mysql" and analyze):
        lines = _postgres(rows)
    else:
        lines = _table(columns, rows)
    return Plan(vendor=vendor, alias=alias, analyzed=analyze, lines=lines)
