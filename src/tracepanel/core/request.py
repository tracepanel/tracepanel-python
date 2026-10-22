"""What a request carried and its response said, as attributes of its trace: headers, query, bodies, cookies, session.

They stay on this machine (the agent keeps them out of what it sends anywhere), and the values under a sensitive name
are masked before they are recorded, as an error's local variables are. A request's body is only what the app read
itself, a response's only one sent whole, each up to `max_body_kb`: tracepanel never reads a stream for the app, nor
holds an upload.
"""

import functools
import json
import re
from collections.abc import Iterable, Mapping
from email.parser import BytesParser
from email.policy import HTTP
from urllib.parse import parse_qsl, quote, urlencode

from tracepanel.core import conf, masking
from tracepanel.core.frames import safe_repr
from tracepanel.core.masking import MASK, card_fields, card_number, counted
from tracepanel.core.model import Attrs

# A response's JSON larger than this isn't parsed to mask its secrets, so isn't kept: its size is.
MAX_PARSED = 4 << 20
# What a response's body shows elsewhere (a page, its assets): kept by its size only.
_SHOWN_ELSEWHERE = frozenset({"text/html", "text/css", "text/javascript", "application/javascript"})
# A form as people read it: encoded, but its spaces, masks and file notes as they are.
_READABLE = "*<>,. /:@[]"
# A response's cookies are a session's keys, whatever their name.
_ALWAYS_MASKED = frozenset({"set-cookie"})
# Session keys that say who is signed in, kept as they are; the hash that proves it stays masked, as a secret.
# Django's user id and backend; Flask-Login's user id and whether the login is fresh.
_SESSION_PLAIN = frozenset({"_auth_user_id", "_auth_user_backend", "_user_id", "_fresh"})
# Flask-Login's session identifier: a hash of the browser's address and agent that guards the session.
_SESSION_MASKED = frozenset({"_id"})


_UNKEYED = re.compile(r"[^a-z0-9_]")
# A form field's object and its own name: card[number] is card's number, card.cvc card's cvc.
_FIELD = re.compile(r"(?P<parent>.+)(?:\[(?P<bracketed>[^\[\]]+)\]|\.(?P<dotted>[^.\[\]]+))")


def key(name: str) -> str:
    """A name as one segment of an attribute's key (content-type -> content_type), as the trace format has keys."""
    return _UNKEYED.sub("_", name.lower()) or "_"


def sensitive(name: str) -> bool:
    """Whether a secret goes under the name: x-api-key, csrftoken, sessionid, password, card[number], cvc."""
    return masking.sensitive(name)


def request_headers(headers: Iterable[tuple[str, str]]) -> Attrs:
    """http.request.header.<name>, the name as a key: several values joined; Cookie is read as cookies instead."""
    sent = [(name, value) for name, value in headers if name.lower() != "cookie" and value]
    # WSGI fills CONTENT_TYPE (text/plain) for a request with no body: no header the client sent.
    if not any(name.lower() == "content-length" for name, _ in sent):
        sent = [(name, value) for name, value in sent if name.lower() != "content-type"]
    return _headers("http.request.header.", sent)


def response_headers(headers: Iterable[tuple[str, str]]) -> Attrs:
    """http.response.header.<name>: several values joined, Set-Cookie masked."""
    return _headers("http.response.header.", headers)


@functools.lru_cache(maxsize=1024)
def _named(prefix: str, name: str) -> tuple[str, bool]:
    """A header's attribute key and whether its value is masked; cached: requests bring the same few names."""
    lowered = name.lower()
    return f"{prefix}{key(lowered)}", lowered in _ALWAYS_MASKED or sensitive(lowered)


def _headers(prefix: str, headers: Iterable[tuple[str, str]]) -> Attrs:
    found: Attrs = {}
    for name, value in headers:
        attr, masked = _named(prefix, name)
        if masked:
            found[attr] = MASK
        elif attr in found:
            found[attr] = f"{found[attr]}, {value}"
        else:
            found[attr] = value
    return found


def query(raw: str) -> Attrs:
    """url.query: the query string, the values of sensitive parameters masked."""
    if not raw:
        return {}
    pairs = parse_qsl(raw, keep_blank_values=True)
    return {"url.query": urlencode([(key, MASK if sensitive(key) else value) for key, value in pairs], safe="*/:")}


def cookies(header: str) -> Attrs:
    """http.request.cookie.<name>, from the Cookie header; a session id or a CSRF token masked."""
    found: Attrs = {}
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            found[f"http.request.cookie.{key(name)}"] = MASK if sensitive(name) else value
    return found


def cookie_values(values: Mapping[str, str]) -> Attrs:
    """http.request.cookie.<name>, from cookies a framework already parsed."""
    return {f"http.request.cookie.{key(name)}": MASK if sensitive(name) else value for name, value in values.items()}


def session(items: Iterable[tuple[str, object]]) -> Attrs:
    """session.<key>: each value's short repr, a sensitive one masked."""
    return {f"session.{key(name)}": _session_value(name, value) for name, value in items}


def user(user_id: object, username: object = None, *, authenticated: bool = True) -> Attrs:
    """user.* of a signed-in user (its id, and its name when the framework has one); an anonymous one has no id.

    The username is local context, as locals are: the agent drops it before anything leaves the machine.
    """
    if not authenticated:
        return {"user.id": None}
    return {"user.id": str(user_id), "user.username": None if username is None else str(username)}


def _session_value(name: str, value: object) -> str:
    if name in _SESSION_MASKED:
        return MASK
    return safe_repr("", value) if name in _SESSION_PLAIN else safe_repr(name, value)


def max_body() -> int:
    """How much of a body is kept, in bytes (and characters): none when bodies aren't captured."""
    config = conf.get_config()
    return max(config.max_body_kb, 0) * 1024 if config.capture_body else 0


def body(content_type: str, data: bytes, *, complete: bool = True) -> Attrs:
    """http.request.body from what the app read: JSON and forms with secrets masked, text as it is, binary not.

    `complete` is False when only the start of the body is at hand: a form or JSON can't be read from a part, and
    a multipart upload past it is not kept.
    """
    limit = max_body()
    if not data or not limit:
        return {}
    text = _readable(content_type, data, complete=complete)
    return {} if text is None else _cut("http.request.body", text, limit)


def response_body(content_type: str, encoding: str, data: bytes) -> Attrs:
    """http.response.body of a response sent whole, as a request's body is read, and its size in bytes.

    A compressed body, a page and its assets, binary content and JSON too large to mask are kept by their size only.
    """
    limit = max_body()
    if not limit:
        return {}
    attrs: Attrs = {"http.response.body.size": len(data)}
    kind = _kind(content_type)
    if not data or encoding.strip().lower() not in {"", "identity"} or kind in _SHOWN_ELSEWHERE:
        return attrs
    if kind == "multipart/form-data" or (_is_json(kind) and len(data) > MAX_PARSED):
        return attrs
    text = _readable(content_type, data, complete=True)
    return attrs if text is None else {**_cut("http.response.body", text, limit), **attrs}


def _kind(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_json(kind: str) -> bool:
    return kind == "application/json" or kind.endswith("+json")


def _readable(content_type: str, data: bytes, *, complete: bool) -> str | None:
    """A body as text: JSON and forms masked, text as it is; None for binary content or a part of a structure."""
    kind = _kind(content_type)
    if _is_json(kind):
        return _json(data) if complete else None
    if kind == "application/x-www-form-urlencoded":
        return _form(data) if complete else None
    if kind == "multipart/form-data":
        return _multipart(content_type, data) if complete else None
    if kind.startswith("text/") or kind in {"application/xml", "application/graphql"} or kind.endswith("+xml"):
        return data.decode("utf-8", errors="replace")
    return None


def form(fields: Iterable[tuple[str, str]], files: Iterable[tuple[str, str, int]]) -> Attrs:
    """http.request.body from a form a framework already parsed: its fields, then its files by name and size."""
    limit = max_body()
    text = _form_text(fields, files) if limit else ""
    return _cut("http.request.body", text, limit) if text else {}


def _form_text(fields: Iterable[tuple[str, str]], files: Iterable[tuple[str, str, int]]) -> str:
    fields = list(fields)
    named = [_field(name) for name, _ in fields]
    # The objects that hold a card's other fields: source[number] beside source[cvc] is the card's number.
    cards = {parent for parent, own in named if card_fields([own])}
    pairs = [
        (name, MASK if sensitive(name) or card_number(own, value, parent, beside_card=parent in cards) else value)
        for (name, value), (parent, own) in zip(fields, named, strict=True)
    ]
    pairs += [(field, _file(name, size)) for field, name, size in files]
    return urlencode(pairs, safe=_READABLE, quote_via=quote)


def _field(name: str) -> tuple[str, str]:
    """A form field's object and its own name: card[number] -> (card, number), a top-level field's object ""."""
    found = _FIELD.fullmatch(name)
    if found is None:
        return "", name
    return found["parent"], found["bracketed"] or found["dotted"]


def _file(name: str, size: int) -> str:
    return f"<file {name}, {size} bytes>"


def _cut(attr: str, text: str, limit: int) -> Attrs:
    if len(text) <= limit:
        return {attr: text}
    return {attr: text[:limit], f"{attr}.truncated": True}


def _json(data: bytes) -> str | None:
    try:
        value = json.loads(data)
    except ValueError:
        return data.decode("utf-8", errors="replace")
    return json.dumps(_masked(value), ensure_ascii=False, separators=(",", ":"))


def _masked(value: object, parent: str | None = None) -> object:
    """A JSON value with the values under sensitive keys masked, at any depth; a card's number in its card."""
    if isinstance(value, dict):
        beside_card = card_fields(value)
        return {
            key: MASK
            if (sensitive(str(key)) and not counted(key, item))
            or card_number(key, item, parent, beside_card=beside_card)
            else _masked(item, str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_masked(item, parent) for item in value]
    return value


def _form(data: bytes) -> str:
    return _form_text(parse_qsl(data.decode("utf-8", errors="replace"), keep_blank_values=True), [])


def _multipart(content_type: str, data: bytes) -> str | None:
    """A multipart form's fields and files, read whole; None when it can't be read as one."""
    message = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode("latin-1") + data)
    if not message.is_multipart():
        return None
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, str, int]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        payload = part.get_payload(decode=True)
        content = payload if isinstance(payload, bytes) else b""
        filename = part.get_filename()
        if filename is not None:
            files.append((name, filename, len(content)))
        else:
            fields.append((name, content.decode("utf-8", errors="replace")))
    return _form_text(fields, files)
