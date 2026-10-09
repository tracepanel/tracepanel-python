"""htmx: what sent a request, how the server answered, and the answers htmx swaps into the page by mistake.

The request's HX-* headers say what sent it, the response's HX-* headers what the server told htmx. Two answers only
show up as a broken page: a redirect (htmx follows it and swaps where it lands) and a whole page instead of a fragment.

Framework-neutral: the framework integrations pass header mappings with lower-case names; importing this module
registers its check with core.analysis.
"""

from collections.abc import Mapping

from tracepanel.core import analysis
from tracepanel.core.model import Attrs, Issue, Trace

# Request header -> attribute. https://htmx.org/reference/#request_headers
REQUEST = {
    "hx-trigger": "htmx.trigger",
    "hx-trigger-name": "htmx.trigger_name",
    "hx-target": "htmx.target",
    "hx-current-url": "htmx.current_url",
    "hx-prompt": "htmx.prompt",
}
# Response header -> attribute. https://htmx.org/reference/#response_headers
RESPONSE = {
    "hx-redirect": "htmx.response.redirect",
    "hx-location": "htmx.response.location",
    "hx-refresh": "htmx.response.refresh",
    "hx-push-url": "htmx.response.push_url",
    "hx-replace-url": "htmx.response.replace_url",
    "hx-retarget": "htmx.response.retarget",
    "hx-reselect": "htmx.response.reselect",
    "hx-reswap": "htmx.response.reswap",
    "hx-trigger": "htmx.response.trigger",
    "hx-trigger-after-settle": "htmx.response.trigger_after_settle",
    "hx-trigger-after-swap": "htmx.response.trigger_after_swap",
}
# Enough of the body to see whether it is a whole document.
SNIFF = 512


def request_attrs(headers: Mapping[str, str]) -> Attrs:
    """`htmx.*` for a request htmx sent (HX-Request: true); nothing for any other."""
    if headers.get("hx-request") != "true":
        return {}
    attrs: Attrs = {"htmx.request": True}
    if headers.get("hx-boosted") == "true":
        attrs["htmx.boosted"] = True
    if headers.get("hx-history-restore-request") == "true":
        attrs["htmx.history_restore"] = True
    for header, name in REQUEST.items():
        if value := headers.get(header):
            attrs[name] = value
    return attrs


def response_attrs(request: Attrs, status: int, headers: Mapping[str, str], body: bytes | None) -> Attrs:
    """What the answer to an htmx request told htmx: HX-* headers, a redirect, a whole page instead of a fragment."""
    if not request.get("htmx.request"):
        return {}
    attrs: Attrs = {}
    for header, name in RESPONSE.items():
        if value := headers.get(header):
            attrs[name] = value
    if 300 <= status < 400 and (location := headers.get("location")):  # noqa: PLR2004
        # The browser follows it inside the XHR: htmx gets (and swaps) whatever the new URL answers.
        attrs["htmx.redirected_to"] = location
    # A boosted link or a history restore asks for the whole page; anything else expects a fragment. htmx swaps only
    # a 2xx by default: an error page (Django's debug 500) never lands in the target.
    page_expected = request.get("htmx.boosted") or request.get("htmx.history_restore")
    swapped = 200 <= status < 300  # noqa: PLR2004
    if swapped and not page_expected and body is not None and _is_document(headers.get("content-type", ""), body):
        attrs["htmx.full_page"] = True
    return attrs


def _is_document(content_type: str, body: bytes) -> bool:
    if not content_type.startswith("text/html"):
        return False
    head = body[:SNIFF].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html"))


REDIRECT_HINT = (
    "The browser follows a redirect inside htmx's request, and htmx swaps the page it lands on into the target. "
    "Answer an htmx request with an `HX-Redirect` (or `HX-Location`) header to navigate the browser, or with a 4xx "
    "the page shows."
)
FULL_PAGE_HINT = (
    "htmx swaps the response into its target element, so a whole page ends up nested in this one. Render a fragment "
    "when the `HX-Request` header is set, or let the link load the page (`hx-boost`)."
)


def issues(trace: Trace) -> list[Issue]:
    """A redirect, or a whole HTML page, answering a request htmx swaps into the page."""
    attrs = trace.attrs
    if not attrs.get("htmx.request"):
        return []
    method = str(attrs.get("http.method") or "GET")
    route = str(attrs.get("http.route") or attrs.get("http.path") or "")
    view = attrs.get("code.view")
    found: list[tuple[str, str, str]] = []
    location = attrs.get("htmx.redirected_to")
    if isinstance(location, str):
        status = attrs.get("http.status_code")
        found.append(("redirect", f"htmx request redirected · {status} to {location}", REDIRECT_HINT))
    if attrs.get("htmx.full_page"):
        found.append(("full-page", f"Whole page sent to htmx · {method} {route}", FULL_PAGE_HINT))
    return [
        Issue(
            fingerprint=analysis.issue_key("htmx", what, method, route),
            fingerprint_version=analysis.FINGERPRINT_VERSION,
            kind="htmx",
            title=title,
            culprit=view if isinstance(view, str) else None,
            hint=hint,
            count=1,
            span_ids=[],
        )
        for what, title, hint in found
    ]


analysis.register_check(issues)
