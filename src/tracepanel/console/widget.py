"""The widget on a framework's pages: response headers for the badge, and the <script> tag in HTML pages.

decorate() does it on plain headers and body, the same for every framework: Django, ASGI and WSGI each turn their
response into those and back. The badge works from the headers even before the panel loads.
"""

import html
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field

from tracepanel.core import runtime
from tracepanel.core.conf import Config
from tracepanel.core.context import now_us
from tracepanel.core.model import Trace
from tracepanel.storage import agent

EXPOSED_HEADERS = ("Tracepanel-Id", "Tracepanel-Name", "Tracepanel-Url", "Server-Timing")

# Issues are found by the agent once the trace is stored: the widget asks the console for them.
PENDING = "pending"

_HEAD = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Timing:
    """The numbers the badge shows for one request."""

    total_ms: float
    db_ms: float
    queries: int

    @classmethod
    def of(cls, trace: Trace) -> "Timing":
        sql = [span for span in trace.spans if span.kind == "sql"]
        return cls(trace.duration / 1000, sum(span.duration for span in sql) / 1000, len(sql))

    @classmethod
    def live(cls, trace: Trace) -> "Timing":
        """For a trace still running (the response goes out before it ends): its time so far."""
        sql = [span for span in trace.spans if span.kind == "sql"]
        total = (now_us() - trace.start) / 1000
        return cls(total, sum(span.duration for span in sql) / 1000, len(sql))


Headers = list[tuple[str, str]]


def header(headers: Headers, name: str) -> str | None:
    lowered = name.lower()
    return next((value for key, value in headers if key.lower() == lowered), None)


def set_header(headers: Headers, name: str, value: str) -> None:
    lowered = name.lower()
    headers[:] = [(key, val) for key, val in headers if key.lower() != lowered]
    headers.append((name, value))


@dataclass(frozen=True, slots=True)
class Page:
    """The response being decorated, as the widget needs it."""

    method: str
    status: int
    # scheme://host of the request, and the console's path on that host (the app's mount point + prefix).
    origin: str
    api: str
    timing: Timing
    charset: str = "utf-8"
    # The request's headers, lower-cased: whether it was the browser going to a page, and what it carried.
    requested: Mapping[str, str] = field(default_factory=dict)
    # Whether a cookie goes out as a Set-Cookie header (ASGI, WSGI) or the framework's own way (Django's cookies).
    cookies_in_headers: bool = True


# The requests that answered a page's navigation with a redirect, by id, on the way to the page the widget will see:
# its panel lists them before that page (a form's POST, then the page it redirected to).
REDIRECTS = "tracepanel_redirect"
# How many redirects in a row are kept: a loop of them doesn't grow the cookie without end.
MAX_REDIRECTS = 5


def navigated(requested: Mapping[str, str]) -> bool:
    """Whether the browser asked for a page to show (a link, a form): not fetch() or an image."""
    mode = requested.get("sec-fetch-mode")
    return mode == "navigate" if mode is not None else "text/html" in requested.get("accept", "")


def redirected(trace: Trace, page: Page) -> str | None:
    """The redirect cookie's value naming this request too; None unless it answers a page's navigation with one."""
    if not (300 <= page.status < 400 and navigated(page.requested)):  # noqa: PLR2004
        return None
    cookies = dict(part.strip().split("=", 1) for part in page.requested.get("cookie", "").split(";") if "=" in part)
    before = [found for found in urllib.parse.unquote(cookies.get(REDIRECTS, "")).split(",") if found]
    return urllib.parse.quote(",".join([*before, trace.id][-MAX_REDIRECTS:]))


def redirect_cookie(trace: Trace, page: Page) -> str | None:
    """The Set-Cookie for the redirect cookie, as a raw header; None when there is none to set."""
    value = redirected(trace, page)
    return None if value is None else f"{REDIRECTS}={value}; Max-Age=30; Path=/; SameSite=Lax"


def decorate(trace: Trace, page: Page, headers: Headers, body: bytes | None, config: Config) -> bytes | None:
    """The badge's headers (in place) and, for an HTML body, the script; returns the body to send.

    body None: a response whose body can't be rewritten (streamed, a file); it still gets the headers.
    """
    set_header(headers, "Tracepanel-Id", trace.id)
    set_header(headers, "Tracepanel-Url", f"{page.origin}{page.api}#/t/{trace.id}")
    if (cookie := redirect_cookie(trace, page)) and page.cookies_in_headers:
        headers.append(("Set-Cookie", cookie))
    if name := display_name(trace, page.method):
        set_header(headers, "Tracepanel-Name", header_value(name))
    existing = header(headers, "Server-Timing")
    timings = server_timing(page.timing)
    set_header(headers, "Server-Timing", f"{existing}, {timings}" if existing else timings)
    if header(headers, "Access-Control-Allow-Origin") is not None:
        expose = exposed(header(headers, "Access-Control-Expose-Headers") or "")
        set_header(headers, "Access-Control-Expose-Headers", expose)
    if body is None:
        return None
    # The panel is the agent's: without one there is nothing to open.
    if config.widget and _html(headers) and agent.ensure_started(config):
        tag = script_tag(page.api, trace, page.method, page.status, page.timing)
        injected = inject(body.decode(page.charset, errors="replace"), tag)
        if injected is not None:
            body = injected.encode(page.charset)
    if header(headers, "Content-Length") is not None:
        set_header(headers, "Content-Length", str(len(body)))
    return body


def _html(headers: Headers) -> bool:
    """An HTML page the script can go into as it is: not compressed."""
    return "text/html" in (header(headers, "Content-Type") or "") and header(headers, "Content-Encoding") is None


def stream(trace: Trace, page: Page, headers: Headers, config: Config) -> "Stream | None":
    """For a body that goes out in pieces: the badge's headers (in place), and what puts the script into it.

    None for anything but an HTML page, or for a page whose size was declared: the script would change it.
    """
    decorate(trace, page, headers, None, config)
    if not (config.widget and _html(headers) and header(headers, "Content-Length") is None):
        return None
    if not agent.ensure_started(config):
        return None
    return Stream(script_tag(page.api, trace, page.method, page.status, page.timing).encode(page.charset))


# Where a streamed page gets the script: right after its <head> opens, or its <body> without one.
_OPENING = re.compile(rb"<(?:head|body)(?:\s[^>]*)?>", re.IGNORECASE)
# A page streamed this far without either is not one the script goes into: the rest passes as it comes.
STREAM_LOOKAHEAD = 65_536


class Stream:
    """The script put into an HTML page that goes out in pieces, as they pass.

    Nothing is held but the end of a piece from its last `<` (a tag that may go on in the next one), until the script
    is in.
    """

    def __init__(self, tag: bytes) -> None:
        self.tag = tag
        self.held = b""
        self.seen = 0
        self.done = False

    def feed(self, piece: bytes) -> bytes:
        """What goes out now, of the piece and what was held."""
        if self.done:
            return piece
        data = self.held + piece
        self.held = b""
        found = _OPENING.search(data)
        if found is not None:
            self.done = True
            return data[: found.end()] + self.tag + data[found.end() :]
        self.seen += len(piece)
        if self.seen > STREAM_LOOKAHEAD:
            self.done = True
            return data
        cut = data.rfind(b"<")
        if cut == -1 or b">" in data[cut:]:
            return data
        self.held = data[cut:]
        return data[:cut]

    def end(self) -> bytes:
        """What was held, once the body ends."""
        rest, self.held, self.done = self.held, b"", True
        return rest


def server_timing(timing: Timing) -> str:
    noun = "query" if timing.queries == 1 else "queries"
    metrics = [
        f'db;dur={timing.db_ms:.1f};desc="{timing.queries} {noun}"',
        f"app;dur={max(timing.total_ms - timing.db_ms, 0):.1f}",
        f"total;dur={timing.total_ms:.1f}",
    ]
    # The agent finds the issues once the trace is stored: the widget asks the console for them.
    metrics.append(f'tracepanel-issues;desc="{PENDING}"')
    return ", ".join(metrics)


def display_name(trace: Trace, method: str) -> str | None:
    """The trace's name when it says more than the request line (`GraphQL query PostList`), else None.

    Requests named after their route (`GET /users/<int:pk>/`) are left to the widget, which shows the actual path.
    """
    return None if trace.name.startswith(f"{method} ") else trace.name


def header_value(text: str) -> str:
    """Header values are Latin-1: names are percent-encoded (the widget decodes them)."""
    return urllib.parse.quote(text, safe=" /:{}<>()[],.-_")


def exposed(existing: str) -> str:
    """Access-Control-Expose-Headers with tracepanel's headers added: cross-origin pages (SPAs) can read them."""
    names = [name.strip() for name in existing.split(",") if name.strip()]
    return ", ".join(dict.fromkeys([*names, *EXPOSED_HEADERS]))


def script_tag(api: str, trace: Trace, method: str, status: int, timing: Timing) -> str:
    """widget.min.js with the page's own request in data-tracepanel-* attributes."""
    name = display_name(trace, method)
    values = {
        "api": api,
        "id": trace.id,
        **({"name": name} if name else {}),
        "method": method,
        "status": status,
        "total": f"{timing.total_ms:.1f}",
        "db": f"{timing.db_ms:.1f}",
        "queries": timing.queries,
        "issues": PENDING,
        # What served the page: the panel shows it next to its logo.
        "runtime": runtime.label(),
    }
    attrs = " ".join(f'data-tracepanel-{name}="{html.escape(str(value))}"' for name, value in values.items())
    return f'<script src="{html.escape(api)}widget.min.js" {attrs}></script>'


def inject(page: str, tag: str) -> str | None:
    """The page with the tag first in <head> (or before </body> without one); None when there is no place.

    First in <head> and not deferred: it must wrap fetch/XHR before the page's own scripts keep a reference to
    them (GraphiQL, Swagger UI, SPA clients).
    """
    head = _HEAD.search(page)
    index = head.end() if head else page.lower().rfind("</body>")
    if index == -1:
        return None
    return page[:index] + tag + page[index:]
