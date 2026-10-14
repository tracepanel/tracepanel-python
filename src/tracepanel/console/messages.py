"""The console's request and response, whatever the framework (or the agent, for forwarded calls)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import IO

# The most a write sends on to the agent (a page's traces from the browser), as the agent itself takes.
MAX_BODY = 8 << 20
# Addresses of this machine: only a caller here may write through the console.
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def is_local(address: str) -> bool:
    return address in LOOPBACK or address.startswith("127.")


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    # Below the prefix: "api/traces", "assets/index-abc.js", "" for the page.
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    # The framework's own request, for the handlers an integration registers.
    native: object = None
    # What a POST sends on to the agent: a page's traces recorded in the browser (`api/ingest`).
    body: bytes = b""
    content_type: str = ""
    # From this machine (or, under Django, INTERNAL_IPS): a write to the agent is taken from such a caller only.
    local: bool = False
    # Its body was over MAX_BODY: not read, answered 413.
    too_large: bool = False


@dataclass(slots=True)
class Response:
    status: int = 200
    content_type: str = "application/json"
    body: bytes = b""
    # A static file, streamed.
    file: IO[bytes] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # A response the framework built itself (it may set cookies): returned as is.
    native: object = None
