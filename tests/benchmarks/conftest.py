import pytest

from tracepanel.core.model import Trace


@pytest.fixture(autouse=True)
def collected(traces: list[Trace]) -> list[Trace]:
    """Traces go to a list, not to an agent: what is measured is the app's side."""
    return traces
