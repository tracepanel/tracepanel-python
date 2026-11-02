"""EXPLAIN for a captured query: the statement for each database, and its plan read back.

The framework runs the statement on the database the query came from (Django: tracepanel.django.explain).

Only statements captured with their parameters (see `capture_sql_params`) that only read are explained: a SELECT, or
a WITH whose CTEs and final statement only read (`refusal`, the rule the agent and every SDK share). `analyze`
(PostgreSQL, MySQL) executes the query to measure it, inside a transaction that is always rolled back.

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

# What a placeholder can't be inside: quoted strings and names (SQL Server's [names] too, not an ARRAY[%s]), and
# comments.
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\W\d][^\]]*\]|--[^\n]*|/\*.*?\*/", re.DOTALL)
# %s, %(name)s, ?, ?1, :name or :1 (not a :: cast), $1, @P1 or @name (not SQL Server's @@ functions).
_PLACEHOLDER = re.compile(r"%s|%\(\w+\)s|\?|(?<!:):[A-Za-z0-9_]|\$\d+|(?<!@)@[A-Za-z0-9_]")
# A named placeholder: %(name)s, or :name (not a :: cast, not Oracle's positional :1).
_NAMED = re.compile(r"%\((\w+)\)s|(?<!:):([A-Za-z_]\w*)")
# What the agent writes before a statement it asks the app to EXPLAIN (tracepanel.storage.agent): the app writes
# its own.
_AGENT_PREFIX = re.compile(r"^\s*EXPLAIN\s+(?:QUERY\s+PLAN\s+|\(ANALYZE,\s*BUFFERS\)\s+|ANALYZE\s+)?", re.IGNORECASE)
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
# The transaction around an EXPLAIN ANALYZE, which runs the query: whatever it did is rolled back. A framework with
# transactions of its own (Django's atomic) runs it in one of those instead.
ROLLBACK_SESSIONS: dict[str, tuple[str, str]] = {
    "postgresql": ("BEGIN", "ROLLBACK"),
    "mysql": ("START TRANSACTION", "ROLLBACK"),
}


# ---------- which statements are explained: the rule the agent and every SDK share ----------

NOT_SELECT = "Only SELECT statements can be explained."
_ONE_STATEMENT = "Only one statement can be explained at a time."
_CHANGING = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
# What can follow a WITH's CTEs: the statement they are for.
_MAIN = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "VALUES", "TABLE"})


@dataclass(slots=True, frozen=True)
class _Token:
    """A word (upper-cased) or any other token (a quoted string or name, a number, a sign), at its depth in ()."""

    text: str
    word: bool
    depth: int


def refusal(statement: str, *, analyze: bool = False) -> str | None:
    """Why a statement isn't explained (with `analyze`, analyzed), or None when it is.

    - It starts (after its comments) with SELECT, or with WITH and ends in a SELECT.
    - It is one statement: nothing follows a `;` but comments.
    - No INSERT, UPDATE, DELETE or MERGE anywhere (a data-modifying CTE), no INTO (`SELECT ... INTO` makes a table).
    - ANALYZE runs it: a SELECT that locks rows (FOR UPDATE, FOR SHARE, LOCK IN SHARE MODE) is explained, not analyzed.

    Its words are read outside quoted strings and names and comments: a keyword in a string changes nothing.
    """
    tokens = _tokens(statement)
    words = [token.text if token.word else "" for token in tokens]
    first = words[0] if words else ""
    if first not in {"SELECT", "WITH"}:
        return NOT_SELECT
    end = next(
        (at for at, token in enumerate(tokens) if token.depth == 0 and token.text == ";" and not token.word), None
    )
    if end is not None and end + 1 < len(tokens):
        return _ONE_STATEMENT
    # The statement a WITH's CTEs are for: the first of its words outside them.
    main = (
        next((at for at, token in enumerate(tokens) if token.word and token.depth == 0 and token.text in _MAIN), None)
        if first == "WITH"
        else 0
    )
    if (why := _changing(words, main)) is not None:
        return why
    if main is None or words[main] != "SELECT":
        return NOT_SELECT
    return _locking(words) if analyze else None


def _previous(words: list[str], at: int) -> str:
    return words[at - 1] if at > 0 else ""


def _locks(words: list[str], at: int) -> bool:
    """Whether UPDATE or SHARE at `at` is a lock's: FOR UPDATE, FOR NO KEY UPDATE, FOR (KEY) SHARE."""
    return _previous(words, at) in {"FOR", "KEY"}


def _changing(words: list[str], main: int | None) -> str | None:
    """Why a statement that writes isn't explained: an INSERT, UPDATE, DELETE or MERGE (a CTE's?), or an INTO."""
    for at, word in enumerate(words):
        if word in _CHANGING and not (word == "UPDATE" and _locks(words, at)):
            inside = " inside WITH" if words[0] == "WITH" and (main is None or at < main) else ""
            return _changes(f"{word}{inside}")
    return _changes("SELECT INTO") if "INTO" in words else None


def _locking(words: list[str]) -> str | None:
    """Why a SELECT that locks rows isn't analyzed."""
    for at, word in enumerate(words):
        if word == "UPDATE" and _locks(words, at):
            locks = "FOR UPDATE"
        elif word == "SHARE" and _locks(words, at):
            locks = "FOR SHARE"
        elif word == "SHARE" and _previous(words, at) == "IN" and _previous(words, at - 1) == "LOCK":
            locks = "LOCK IN SHARE MODE"
        else:
            continue
        return f"This query locks rows ({locks}): it can be explained, not analyzed."
    return None


def _changes(what: str) -> str:
    return f"This query changes data ({what}): it isn't explained."


def _tokens(statement: str) -> list[_Token]:  # noqa: C901, PLR0912 (one branch per kind of token)
    """The statement's tokens, without its comments and spaces.

    A quoted string or name ('...' with '', E'...' with backslashes too, "...", `...`, [...], PostgreSQL's
    $tag$...$tag$) is one token whose text is never read. The inside of MySQL's /*! ... */, which MySQL runs, is read.
    """
    chars = statement
    tokens: list[_Token] = []
    depth = 0
    at = 0
    size = len(chars)
    while at < size:
        char = chars[at]
        following = chars[at + 1] if at + 1 < size else ""
        if char.isspace():
            at += 1
            continue
        if char == "-" and following == "-":
            at = _past(chars, at + 2, "\n")
            continue
        if char == "/" and following == "*":
            at = at + 3 if chars[at + 2 : at + 3] == "!" else _past(chars, at + 2, "*/")
            continue
        if char == "'":
            # E'...' (PostgreSQL's escape string): a backslash escapes the next character.
            escapes = at > 0 and chars[at - 1] in "Ee" and bool(tokens) and tokens[-1].word and tokens[-1].text == "E"
            at = _quoted(chars, at, "'", escapes=escapes)
            text, word = "'", False
        elif char in '"`':
            at = _quoted(chars, at, char, escapes=False)
            text, word = char, False
        elif char == "[" and (following.isalpha() or following == "_"):
            at = _past(chars, at + 1, "]")
            text, word = "[", False
        elif char == "$" and (tag := _dollar_tag(chars, at)) is not None:
            at = _past(chars, at + len(tag), tag)
            text, word = "$", False
        elif char.isalpha() or char == "_":
            start = at
            while at < size and (chars[at].isalnum() or chars[at] in "_$"):
                at += 1
            text, word = chars[start:at].upper(), True
        elif "0" <= char <= "9":
            while at < size and (chars[at].isalnum() or chars[at] in "_."):
                at += 1
            text, word = "0", False
        else:
            at += 1
            text, word = char, False
        if text == ")" and not word:
            depth = max(depth - 1, 0)
        tokens.append(_Token(text, word, depth))
        if text == "(" and not word:
            depth += 1
    return tokens


def _quoted(chars: str, at: int, quote: str, *, escapes: bool) -> int:
    """Where a quote opened at `at` ends: past its closing `quote` (doubled, it is one inside), or the end."""
    at += 1
    while at < len(chars):
        char = chars[at]
        if (escapes and char == "\\") or (char == quote and chars[at + 1 : at + 2] == quote):
            at += 2
        elif char == quote:
            return at + 1
        else:
            at += 1
    return len(chars)


def _past(chars: str, start: int, end: str) -> int:
    """Past the first `end` from `start` on, or the statement's end."""
    found = chars.find(end, start)
    return len(chars) if found < 0 else found + len(end)


def _dollar_tag(chars: str, at: int) -> str | None:
    """PostgreSQL's dollar quote opening at `at`: $$ or $tag$ (not a $1 parameter)."""
    end = at + 1
    while end < len(chars):
        char = chars[end]
        if char == "$":
            return chars[at : end + 1]
        first = end == at + 1
        if not (char.isalpha() or char == "_" or (not first and "0" <= char <= "9")):
            return None
        end += 1
    return None


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
    # What the database answered, as it came: the agent reads its own plan from them (tracepanel.storage.agent).
    columns: list[str] = field(default_factory=list, compare=False, repr=False)
    rows: list[tuple[object, ...]] = field(default_factory=list, compare=False, repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "alias": self.alias,
            "analyzed": self.analyzed,
            "lines": [{"depth": line.depth, "text": line.text, "warning": line.warning} for line in self.lines],
        }

    def to_answer(self) -> dict[str, object]:
        """The answer to the agent's request: the plan as the console shows it, and the rows it reads its own from."""
        return {
            **self.to_dict(),
            "columns": self.columns,
            "rows": [[_scalar(value) for value in row] for row in self.rows],
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
    """A plan as text (PostgreSQL's, MySQL's ANALYZE): a line sits a level below the nearest node above, indented less.

    Nodes are the "->" lines and the top ones. PostgreSQL indents a level by 6 and a node's details (Filter:, Hash
    Cond:) by 6 past its arrow; MySQL's tree, a single row, indents a level by 4.
    """
    lines: list[PlanLine] = []
    nodes: list[tuple[int, int]] = []  # The open nodes, outermost first: (indent, depth).
    for (row,) in rows:
        for text in str(row).splitlines():
            indent = len(text) - len(text.lstrip())
            node = text.strip().startswith("->")
            body = text.strip().removeprefix("->").strip()
            while nodes and nodes[-1][0] >= indent:
                nodes.pop()
            depth = nodes[-1][1] + 1 if nodes else 0
            if node or indent == 0:
                nodes.append((indent, depth))
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


def asked(request: Mapping[str, object]) -> tuple[str, list[Scalar] | dict[str, Scalar] | None, str, bool]:
    """What the agent asks the app to EXPLAIN: the statement, its parameters, its alias, whether to ANALYZE.

    The agent sends the statement it wrote for the database, `EXPLAIN ...`: the app writes its own, as for its console
    (SQL Server's has none). Its parameters are the span's, a list: named ones go back to their names, as the cursor
    took them, from `param_names` when the agent sends them, else from the statement's placeholders in order.
    """
    sql, params, names = request.get("sql"), request.get("params"), request.get("param_names")
    if not isinstance(sql, str):
        msg = "The agent asked for no statement."
        raise ExplainError(msg)
    statement = _AGENT_PREFIX.sub("", sql, count=1)
    values = [_scalar(value) for value in params] if isinstance(params, list) else None
    if values is not None and not isinstance(names, list):
        names = list(dict.fromkeys(a or b for a, b in _NAMED.findall(_QUOTED.sub("", statement)))) or None
    if values is not None and isinstance(names, list) and len(names) == len(values):
        values = dict(zip((str(name) for name in names), values, strict=True))
    return statement, values, str(request.get("alias") or "default"), request.get("analyze") is True


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


def explain(  # noqa: C901, PLR0912, PLR0913 (the statement, where it ran, how to run it: a branch per database)
    statement: str,
    params: Sequence[Scalar] | Mapping[str, Scalar] | None,
    *,
    vendor: str,
    alias: str,
    execute: Execute,
    analyze: bool = False,
) -> Plan:
    if (why := refusal(statement)) is not None:
        raise ExplainError(why)
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
    if analyze:
        if (why := refusal(statement, analyze=True)) is not None:
            raise ExplainError(why)
        session = ROLLBACK_SESSIONS[vendor]
    rows, columns = execute(prefix + statement, params, session)
    if session is MSSQL_SESSION:
        lines = _mssql(columns, rows)
    elif vendor == "sqlite":
        lines = _sqlite(rows)
    elif vendor == "postgresql" or (vendor == "mysql" and analyze):
        lines = _postgres(rows)
    else:
        lines = _table(columns, rows)
    return Plan(vendor=vendor, alias=alias, analyzed=analyze, lines=lines, columns=columns, rows=rows)
