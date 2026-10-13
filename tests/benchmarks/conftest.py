from collections.abc import Generator

import pytest

from tracepanel.core.model import Trace
from tracepanel.storage import transport


class Discard:
    """A writer that drops every trace: benchmarks run many requests, keeping them all would only grow memory."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, trace: Trace) -> None:
        del trace
        self.submitted += 1

    def flush(self, timeout: float = 5.0) -> None:
        pass


@pytest.fixture
def discard() -> Generator[Discard, None, None]:
    writer = Discard()
    previous = transport._writer  # noqa: SLF001
    transport.set_writer(writer)
    yield writer
    transport.set_writer(previous)
