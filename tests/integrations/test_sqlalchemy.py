import contextvars
import time

import pytest
from dirty_equals import IsPartialDict, IsStr

sqlalchemy = pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text  # noqa: E402 (optional test group)
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from tests.agentstore import issues, untimed  # noqa: E402
from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Trace  # noqa: E402
from tracepanel.integrations import sqlalchemy as integration  # noqa: E402
from tracepanel.storage.transport import complete  # noqa: E402

integration.install()


def memory(**kwargs: object) -> sqlalchemy.Engine:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, **kwargs)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO item (name) VALUES ('a'), ('b')"))
    return engine


def traced(work: object) -> Trace:
    with start_trace("http", "GET /") as trace:
        work()  # type: ignore[operator]
    return complete(trace)


def test_two_databases_with_the_same_name_stay_apart(traces: list[Trace]) -> None:
    first, second = memory(), memory()
    second_alias = integration.engine_alias(second)

    trace = traced(lambda: [engine.connect().execute(text("SELECT name FROM item")) for engine in (first, second)])

    aliases = [span.attrs["db.alias"] for span in trace.spans if span.kind == "sql"]
    assert aliases == [integration.engine_alias(first), second_alias]
    assert len(set(aliases)) == 2
    assert second_alias == IsStr(regex=r"sqlite(#\d+)?")


def test_logging_name_names_the_database(traces: list[Trace]) -> None:
    replica = memory(logging_name="replica")

    trace = traced(lambda: replica.connect().execute(text("SELECT name FROM item")))

    assert [span.attrs["db.alias"] for span in trace.spans if span.kind == "sql"] == ["replica"]
    plan = integration.explain("SELECT name FROM item WHERE id = ?", [1], "replica")
    assert plan.to_dict() == IsPartialDict(vendor="sqlite", alias="replica")


def test_transaction_holds_its_statements(traces: list[Trace]) -> None:
    engine = memory(logging_name="shop")

    def work() -> None:
        with Session(engine) as session, session.begin():
            session.execute(text("UPDATE item SET name = 'c' WHERE id = 1"))
            savepoint = session.begin_nested()
            session.execute(text("UPDATE item SET name = 'd' WHERE id = 2"))
            savepoint.rollback()

    trace = traced(work)
    transaction, savepoint = [span for span in trace.spans if span.kind == "transaction"]
    updates = [span for span in trace.spans if span.kind == "sql" and span.name.startswith("UPDATE")]

    assert (transaction.name, transaction.attrs) == (
        "transaction shop",
        IsPartialDict(
            {
                "db.client": "sqlalchemy",
                "db.alias": "shop",
                "db.transaction.savepoint": False,
                "db.transaction.outcome": "commit",
                "code.origin": IsStr(regex=r"tests/integrations/test_sqlalchemy\.py:\d+ in work"),
            }
        ),
    )
    assert (savepoint.parent_id, savepoint.attrs["db.transaction.outcome"]) == (transaction.id, "rollback")
    assert [span.parent_id for span in updates] == [transaction.id, savepoint.id]


def test_statements_join_the_transaction_open_on_their_connection(traces: list[Trace]) -> None:
    engine = memory()

    def work() -> None:
        with Session(engine) as session:
            # The transaction begins in a copy of this context (a worker thread's): its span is current only there.
            contextvars.copy_context().run(session.execute, text("SELECT 1"))
            session.execute(text("SELECT name FROM item"))
            session.commit()

    trace = traced(work)
    root, transaction, *_ = trace.spans

    assert [(span.kind, span.parent_id) for span in trace.spans] == [
        ("http", None),
        ("transaction", root.id),
        ("sql", transaction.id),
        ("sql", transaction.id),
    ]
    assert transaction.attrs["db.transaction.outcome"] == "commit"


def test_session_closed_without_commit_ends_its_transaction(traces: list[Trace]) -> None:
    engine = create_engine("sqlite://")

    def work() -> None:
        with Session(engine) as session:
            session.execute(text("SELECT 1"))

    trace = traced(work)
    (transaction,) = [span for span in trace.spans if span.kind == "transaction"]

    assert transaction.attrs["db.transaction.outcome"] == "rollback"
    # The trace's own span stack is back to the root: nothing hangs under the closed transaction.
    assert trace.spans[0].parent_id is None


def test_autocommit_holds_no_transaction(traces: list[Trace]) -> None:
    engine = memory()

    def work() -> None:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("SELECT name FROM item"))
        with create_engine("sqlite://", isolation_level="AUTOCOMMIT").connect() as connection:
            connection.execute(text("SELECT 1"))

    trace = traced(work)

    # Each statement commits on its own: a call between them runs outside any transaction.
    assert [span.kind for span in trace.spans] == ["http", "sql", "sql"]


def test_long_transaction_gets_a_sqlalchemy_hint(traces: list[Trace]) -> None:
    engine = memory()

    def work() -> None:
        with engine.begin() as connection:
            connection.execute(text("SELECT name FROM item"))
            time.sleep(0.55)

    trace = traced(work)
    (issue,) = [issue for issue in issues(trace) if issue.kind == "long-transaction"]

    assert issue.hint == IsStr(regex=r".*commit first, or do it before the `with session.begin\(\)`.*")


def returned(trace: Trace) -> list[object]:
    return [span.attrs.get("db.response.returned_rows") for span in trace.spans if span.kind == "sql"]


def test_the_rows_a_result_gave_are_counted(traces: list[Trace]) -> None:
    engine = memory()

    def work() -> None:
        with engine.connect() as connection:
            connection.execute(text("SELECT name FROM item")).all()
            connection.execute(text("SELECT name FROM item")).first()
            list(connection.execute(text("SELECT name FROM item")))
            connection.execute(text("SELECT name FROM item")).scalars().all()
            connection.execute(text("UPDATE item SET name = 'c'"))

    assert returned(traced(work)) == [2, 1, 2, 2, None]


def test_the_agent_finds_a_select_without_a_limit_that_read_many_rows(traces: list[Trace]) -> None:
    engine = memory()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO item (name) VALUES (:name)"), [{"name": str(n)} for n in range(1200)])

    def work() -> None:
        with Session(engine) as session:
            session.execute(text("SELECT id, name FROM item")).all()
            session.execute(text("SELECT id, name FROM item LIMIT 1100")).all()

    # Timed issues left out: a loaded machine holds the session's transaction open past its limit.
    assert [(issue.kind, issue.title) for issue in untimed(traced(work))] == [
        ("large-result", "Large result: 1202 rows · SELECT item"),
    ]
