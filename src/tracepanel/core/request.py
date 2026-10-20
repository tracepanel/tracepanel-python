"""What a request carried, as attributes of its trace: headers, query, body, cookies, the session.

They stay on this machine (the agent keeps them out of what it sends anywhere), and the values under a sensitive name
are masked before they are recorded, as an error's local variables are. A body is only what the app read itself,
up to MAX_BODY characters: tracepanel never reads a stream for the app, nor holds an upload.
"""

import json
import re
from collections.abc import Iterable, Mapping
from email.parser import BytesParser
from email.policy import HTTP
from urllib.parse import parse_qsl, quote, urlencode

from tracepanel.core.frames import MASK, SENSITIVE, safe_repr
from tracepanel.core.model import Attrs

# How much of a body is kept, in characters.
MAX_BODY = 16_384
# A form as people read it: encoded, but its spaces, masks and file notes as they are.
_READABLE = "*<>,. /:@"
# A response's cookies are a session's keys, whatever their name.
_ALWAYS_MASKED = frozenset({"set-cookie"})
# Django's own session keys: the user's id and backend say who is signed in; the hash proves it, so it is a secret.
_SESSION_PLAIN = frozenset({"_auth_user_id", "_auth_user_backend"})


_UNKEYED = re.compile(r"[^a-z0-9_]")


def key(name: str) -> str:
    """A name as one segment of an attribute's key (content-type -> content_type), as the trace format has keys."""
    return _UNKEYED.sub("_", name.lower()) or "_"


def sensitive(name: str) -> bool:
    """Whether a secret goes under the name: x-api-key, csrftoken, sessionid, password."""
    return SENSITIVE.search(name.replace("-", "_")) is not None


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


def _headers(prefix: str, headers: Iterable[tuple[str, str]]) -> Attrs:
    values: dict[str, list[str]] = {}
    for name, value in headers:
        values.setdefault(name.lower(), []).append(value)
    return {
        f"{prefix}{key(name)}": MASK if name in _ALWAYS_MASKED or sensitive(name) else ", ".join(found)
        for name, found in values.items()
    }


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
    return {
        f"session.{key(name)}": safe_repr("", value) if name in _SESSION_PLAIN else safe_repr(name, value)
        for name, value in items
    }


def body(content_type: str, data: bytes, *, complete: bool = True) -> Attrs:
    """http.request.body from what the app read: JSON and forms with secrets masked, text as it is, binary not.

    `complete` is False when only the start of the body is at hand: a form or JSON can't be read from a part, and
    a multipart upload past it is not kept.
    """
    kind = content_type.split(";", 1)[0].strip().lower()
    text: str | None
    if not data:
        return {}
    if kind == "application/json" or kind.endswith("+json"):
        text = _json(data) if complete else None
    elif kind == "application/x-www-form-urlencoded":
        text = _form(data) if complete else None
    elif kind == "multipart/form-data":
        text = _multipart(content_type, data) if complete else None
    elif kind.startswith("text/") or kind in {"application/xml", "application/graphql"}:
        text = data.decode("utf-8", errors="replace")
    else:
        return {}
    if text is None:
        return {}
    return _cut(text)


def form(fields: Iterable[tuple[str, str]], files: Iterable[tuple[str, str, int]]) -> Attrs:
    """http.request.body from a form a framework already parsed: its fields, then its files by name and size."""
    text = _form_text(fields, files)
    return _cut(text) if text else {}


def _form_text(fields: Iterable[tuple[str, str]], files: Iterable[tuple[str, str, int]]) -> str:
    pairs = [(key, MASK if sensitive(key) else value) for key, value in fields]
    pairs += [(field, _file(name, size)) for field, name, size in files]
    return urlencode(pairs, safe=_READABLE, quote_via=quote)


def _file(name: str, size: int) -> str:
    return f"<file {name}, {size} bytes>"


def _cut(text: str) -> Attrs:
    if len(text) <= MAX_BODY:
        return {"http.request.body": text}
    return {"http.request.body": text[:MAX_BODY], "http.request.body.truncated": True}


def _json(data: bytes) -> str | None:
    try:
        value = json.loads(data)
    except ValueError:
        return data.decode("utf-8", errors="replace")
    return json.dumps(_masked(value), ensure_ascii=False, separators=(",", ":"))


def _masked(value: object) -> object:
    """A JSON value with the values under sensitive keys masked, at any depth."""
    if isinstance(value, dict):
        return {key: MASK if sensitive(str(key)) else _masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
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
