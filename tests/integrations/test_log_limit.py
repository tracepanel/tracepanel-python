"""A trace keeps its first records up to the limit every SDK and the agent share, and counts the rest."""

import logging

from tracepanel.core.context import start_trace
from tracepanel.integrations.logging import MAX_LOGS


def test_a_trace_keeps_its_first_records_and_counts_the_rest() -> None:
    logger = logging.getLogger("tests.log_limit")
    logger.setLevel(logging.INFO)

    with start_trace("http", "GET /loop") as trace:
        for n in range(MAX_LOGS + 5):
            logger.info("line %d", n)

    assert MAX_LOGS == 1000
    assert len(trace.logs) == MAX_LOGS
    assert trace.logs[-1].message == f"line {MAX_LOGS - 1}"
    assert trace.attrs["tracepanel.dropped_logs"] == 5
