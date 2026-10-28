"""Tracepanel: see what your app does on every request.

Instrument your own code with `tracepanel.span()`, `@tracepanel.traced`, `tracepanel.trace()`, `tracepanel.set_attr()`
and `tracepanel.event()`; see tracepanel.api. In tests, `tracepanel.max_queries()`: see tracepanel.testing.
"""

from tracepanel.api import SpanHandle, event, set_attr, span, trace, traced
from tracepanel.testing import max_queries

__all__ = ["SpanHandle", "event", "max_queries", "set_attr", "span", "trace", "traced"]
__version__ = "0.6.0"
