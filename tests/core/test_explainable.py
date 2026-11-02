"""Which statements EXPLAIN runs: the cases the agent and every SDK share (the agent's schema/explainable.json)."""

import json
from pathlib import Path
from typing import Any

import pytest

from tracepanel.core import explain as plans

CASES: list[dict[str, Any]] = json.loads((Path(__file__).parent / "explainable.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[case["sql"][:60] for case in CASES])
def test_the_shared_cases(case: dict[str, Any]) -> None:
    refusals = {
        "sql": case["sql"],
        "explain": plans.refusal(case["sql"]),
        "analyze": plans.refusal(case["sql"], analyze=True),
    }

    assert refusals == case


def test_a_data_modifying_cte_is_never_run() -> None:
    ran: list[str] = []

    def execute(sql: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
        ran.append(sql)
        return [], []

    with pytest.raises(
        plans.ExplainError, match=r"^This query changes data \(DELETE inside WITH\): it isn't explained\.$"
    ):
        plans.explain(
            "WITH gone AS (DELETE FROM t RETURNING id) SELECT * FROM gone",
            [],
            vendor="postgresql",
            alias="default",
            execute=execute,
            analyze=True,
        )

    assert ran == []


@pytest.mark.parametrize(
    ("vendor", "session"),
    [("postgresql", ("BEGIN", "ROLLBACK")), ("mysql", ("START TRANSACTION", "ROLLBACK"))],
)
def test_analyze_runs_in_a_transaction_rolled_back(vendor: str, session: plans.Session) -> None:
    ran: list[tuple[str, plans.Session]] = []

    def execute(sql: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
        ran.append((sql, session))
        return [], ["QUERY PLAN"]

    plans.explain(
        "WITH a AS (SELECT 1) SELECT * FROM a", [], vendor=vendor, alias="default", execute=execute, analyze=True
    )
    plans.explain("WITH a AS (SELECT 1) SELECT * FROM a", [], vendor=vendor, alias="default", execute=execute)

    assert ran == [
        (
            ("EXPLAIN (ANALYZE, BUFFERS) " if vendor == "postgresql" else "EXPLAIN ANALYZE ")
            + "WITH a AS (SELECT 1) SELECT * FROM a",
            session,
        ),
        ("EXPLAIN WITH a AS (SELECT 1) SELECT * FROM a", None),
    ]


def test_a_locking_select_is_explained_not_analyzed() -> None:
    def execute(sql: str, values: plans.Values, session: plans.Session) -> tuple[plans.Rows, list[str]]:
        return [], ["QUERY PLAN"]

    plan = plans.explain("SELECT * FROM t FOR UPDATE", [], vendor="postgresql", alias="default", execute=execute)

    assert plan.to_dict() == {"vendor": "postgresql", "alias": "default", "analyzed": False, "lines": []}

    with pytest.raises(
        plans.ExplainError, match=r"^This query locks rows \(FOR UPDATE\): it can be explained, not analyzed\.$"
    ):
        plans.explain(
            "SELECT * FROM t FOR UPDATE", [], vendor="postgresql", alias="default", execute=execute, analyze=True
        )
