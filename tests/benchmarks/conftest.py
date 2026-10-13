from collections.abc import Generator

import pytest

from tracepanel.core.model import Trace
from tracepanel.storage import transport


class Discard:
    """A writer that drops every trace: a benchmark runs thousands of requests, and keeping them would grow memory."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, trace: Trace) -> None:
        del trace
        self.submitted += 1

    def flush(self, timeout: float = 5.0) -> None:
        pass


@pytest.fixture(autouse=True)
def discard() -> Generator[Discard, None, None]:
    """Traces go nowhere, never to an agent: what is measured is the app's side."""
    writer = Discard()
    previous = transport._writer  # noqa: SLF001
    transport.set_writer(writer)
    yield writer
    transport.set_writer(previous)
