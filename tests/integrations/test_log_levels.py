"""A record's level is one of the five every SDK shares, whatever level the app's logging gave it."""

import logging

from tracepanel.core.context import start_trace


def test_a_level_of_its_own_falls_in_by_its_number() -> None:
    logger = logging.getLogger("tests.log_levels")
    logger.setLevel(1)
    # A library's own levels: below debug, between info and warning, the standard ones, and past critical.
    custom = {"TRACE": 5, "SUCCESS": 25, "NOTICE": 35, "FATAL": 60}

    with start_trace("http", "GET /levels") as trace:
        for number in (*custom.values(), logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
            logger.log(number, "at %d", number)

    assert [(log.message, log.level) for log in trace.logs] == [
        ("at 5", "debug"),
        ("at 25", "info"),
        ("at 35", "warning"),
        ("at 60", "critical"),
        ("at 10", "debug"),
        ("at 20", "info"),
        ("at 30", "warning"),
        ("at 40", "error"),
        ("at 50", "critical"),
    ]
