"""The console's request and response, whatever the framework (or the agent, for forwarded calls)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import IO


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
