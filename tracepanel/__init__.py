"""tracepanel — see what your Django app is really doing.

Instrument your own code with `tracepanel.span()`, `@tracepanel.traced`, `tracepanel.trace()`, `tracepanel.set_attr()`
and `tracepanel.event()`; see tracepanel.api.
"""

from tracepanel.api import SpanHandle, event, set_attr, span, trace, traced

__all__ = ["SpanHandle", "event", "set_attr", "span", "trace", "traced"]
__version__ = "0.0.1"
