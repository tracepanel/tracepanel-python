from tracepanel.core import w3c
from tracepanel.core.model import Trace

# The W3C spec's example.
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_a_ulid_is_128_bits_a_span_id_its_last_64() -> None:
    # 01ARZ3NDEKTSV4RRFFQ69G5FAV is 0x01563E3AB5D3D6764C61EFB99302BD5B.
    assert w3c.span_id("01ARZ3NDEKTSV4RRFFQ69G5FAV") == "4c61efb99302bd5b"

    trace = Trace(id="01ARZ3NDEKTSV4RRFFQ69G5FAV", kind="http", name="GET /", start=0)
    assert w3c.trace_id(trace) == "01563e3ab5d3d6764c61efb99302bd5b"
    assert trace.attrs == {"w3c.trace_id": "01563e3ab5d3d6764c61efb99302bd5b"}


def test_a_call_carries_the_trace_it_was_called_in() -> None:
    trace = Trace(id="01ARZ3NDEKTSV4RRFFQ69G5FAV", kind="http", name="GET /", start=0, attrs=w3c.incoming(TRACEPARENT))

    assert w3c.outgoing(trace, "01ARZ3NDEKTSV4RRFFQ69G5FAV") == (
        "00-4bf92f3577b34da6a3ce929d0e0e4736-4c61efb99302bd5b-01"
    )


def test_an_incoming_traceparent() -> None:
    assert w3c.incoming(TRACEPARENT) == {
        "w3c.trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "w3c.parent_span_id": "00f067aa0ba902b7",
    }
    assert w3c.incoming(TRACEPARENT.upper().encode()) == w3c.incoming(TRACEPARENT)
    # A later version is read as 00, its extra fields ignored.
    assert w3c.incoming(f"01{TRACEPARENT[2:]}-extra") == w3c.incoming(TRACEPARENT)

    # What the spec says to ignore: none, nonsense, version ff, an all-zero trace or span.
    for header in (
        None,
        "",
        "nonsense",
        f"ff{TRACEPARENT[2:]}",
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
    ):
        assert w3c.incoming(header) == {}
