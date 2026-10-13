"""W3C trace context (`traceparent`): what links a request one app makes to the trace of the app that serves it.

A call out carries `traceparent: 00-<trace id>-<span id>-01`: the W3C trace (the one the request came in with, else
this trace's own id, its 128 bits in hex) and the call's span id (the last 64 bits of its id). The call's span keeps
that span id as `w3c.span_id`, and its trace the W3C trace as `w3c.trace_id`. The app it reaches reads the header
into its trace's `w3c.trace_id` and `w3c.parent_span_id`. The agent finds the call whose span id is the other trace's
parent, in the same W3C trace: one trace is `called_by` the other, whichever app recorded it, an SDK or OpenTelemetry.
"""

import re

from tracepanel.core.model import Attrs, Trace

HEADER = "traceparent"
# version 00 (and any later one read as 00, as the spec asks): trace id, parent span id, flags. All zeros is invalid.
_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_ZERO_TRACE, _ZERO_SPAN = "0" * 32, "0" * 16
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DIGITS = {char: value for value, char in enumerate(_CROCKFORD)}


def _bits(ulid: str) -> int:
    """A ULID's 128 bits."""
    value = 0
    for char in ulid:
        value = value << 5 | _DIGITS[char]
    return value


def span_id(span: str) -> str:
    """The W3C span id of a span: the last 64 bits of its ULID, in hex."""
    return f"{_bits(span) & (1 << 64) - 1:016x}"


def trace_id(trace: Trace) -> str:
    """The W3C trace this trace is part of: the one it was called in, else its own id, kept on it from now on."""
    known = trace.attrs.get("w3c.trace_id")
    if isinstance(known, str):
        return known
    own = f"{_bits(trace.id):032x}"
    trace.attrs["w3c.trace_id"] = own
    return own


def outgoing(trace: Trace, span: str) -> str:
    """The `traceparent` a call made in `trace` carries, whose span is `span`."""
    return f"00-{trace_id(trace)}-{span_id(span)}-01"


def incoming(header: str | bytes | None) -> Attrs:
    """A request's `traceparent`, as its trace's attributes: the W3C trace and the caller's span; none if invalid."""
    if isinstance(header, bytes):
        header = header.decode("latin-1")
    match = _TRACEPARENT.match((header or "").strip().lower())
    if match is None or match[1] == "ff" or match[2] == _ZERO_TRACE or match[3] == _ZERO_SPAN:
        return {}
    return {"w3c.trace_id": match[2], "w3c.parent_span_id": match[3]}
