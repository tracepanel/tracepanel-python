"""Values as a reader sees them, secrets masked: an app's settings and the context a template got.

Both are repr()-ed and cut the way an error's local variables are (reprlib), after a masked copy is made: a value under
a sensitive name is MASK at any depth (DATABASES' PASSWORD, a dict's `api_key`), and the credentials in a URL
(`postgres://shop:pw@db/shop`, a Sentry DSN's key) are MASK too, whatever its name. Names are judged by a rule of the
caller's: settings by SETTING_SENSITIVE, a template's context by the locals' SENSITIVE.

A framework can say how to show its own objects without touching them (Django's QuerySet, which a repr() would run)
with summarize(), and guard() what a repr() may do (Django blocks queries meanwhile).
"""

import functools
import json
import re
import reprlib
from collections.abc import Callable, Mapping
from typing import TypeVar

from tracepanel._compat import override
from tracepanel.core import conf
from tracepanel.core.frames import MASK, SENSITIVE
from tracepanel.core.model import Attrs, Scalar, Trace

# Settings' own names: Django's idea of a hidden setting. The locals' rule would mask SESSION_COOKIE_AGE,
# AUTH_USER_MODEL and CSRF_COOKIE_NAME, which say nothing secret.
SETTING_SENSITIVE = re.compile(r"secret|token|pass|key|signature|private|credential", re.IGNORECASE)
# Django's own settings whose names look secret but whose values aren't.
SETTING_PLAIN = frozenset(
    {"PASSWORD_HASHERS", "AUTH_PASSWORD_VALIDATORS", "PASSWORD_RESET_TIMEOUT", "KEY_PREFIX", "KEY_FUNCTION"}
)
# `scheme://userinfo@`: a URL's user and password, or a key alone (a Sentry DSN).
_USERINFO = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^\s/@]+)@")
# How deep a masked copy goes: past it, a value is repr()-ed as it is (reprlib stops sooner anyway).
_MAX_DEPTH = 8


@functools.lru_cache(maxsize=4096)
def setting_sensitive(name: str) -> bool:
    """Whether a setting (or a key inside one) holds a secret: SECRET_KEY, PASSWORD, STRIPE_API_KEY, AWS_TOKEN."""
    return name.upper() not in SETTING_PLAIN and SETTING_SENSITIVE.search(name) is not None


@functools.lru_cache(maxsize=4096)
def local_sensitive(name: str) -> bool:
    """The rule of an error's local variables: password, token, csrf_token, session, api_key."""
    return SENSITIVE.search(name) is not None


class _Shown:
    """A stand-in whose repr is given: MASK, or a framework's summary of an object it must not touch."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


_MASKED = _Shown(MASK)

Summarizer = Callable[[object], str | None]
_summarizers: list[Summarizer] = []
# Each blocks what a repr() must not do and returns what undoes it: plain calls, as a capture can run per render.
Guard = Callable[[], Callable[[], None]]
_guards: list[Guard] = []


def summarize(summarizer: Summarizer) -> None:
    """Show some objects by a summary of a framework's instead of their repr(): None leaves an object to repr()."""
    if summarizer not in _summarizers:
        _summarizers.append(summarizer)


def guard(block: Guard) -> None:
    """What every capture runs under: Django's blocks queries, so no repr() runs one."""
    if block not in _guards:
        _guards.append(block)


T = TypeVar("T")


def guarded(run: Callable[[], T]) -> T:
    """run() under every guard."""
    undo = [block() for block in _guards]
    try:
        return run()
    finally:
        for each in reversed(undo):
            each()


def mask_url(text: str) -> str:
    """The text with every URL's credentials masked: `redis://:pw@cache:6379/0` -> `redis://********@cache:6379/0`."""
    return _USERINFO.sub(lambda match: f"{match['scheme']}{MASK}@", text) if "@" in text else text


def _summary(value: object) -> str | None:
    for summarizer in _summarizers:
        shown = summarizer(value)
        if shown is not None:
            return shown
    return None


def masked(value: object, sensitive: Callable[[str], bool], depth: int = 0) -> object:
    """A copy of `value` fit to repr(): secrets masked, framework objects summarized; the rest as it is."""
    if isinstance(value, str):
        return mask_url(value)
    plain = isinstance(value, (int, float, bool, bytes)) or value is None
    shown = None if plain else _summary(value)
    if shown is not None:
        return _Shown(shown)
    if plain or depth >= _MAX_DEPTH:
        return value
    if isinstance(value, Mapping):
        return {
            key: _MASKED if isinstance(key, str) and sensitive(key) else masked(item, sensitive, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return _masked_items(value, [masked(item, sensitive, depth + 1) for item in value])
    return value


def _masked_items(
    value: list[object] | tuple[object, ...] | set[object] | frozenset[object], items: list[object]
) -> object:
    """A list, tuple or set made again of its masked items."""
    if isinstance(value, list):
        return items
    if isinstance(value, tuple):
        return tuple(items)
    # A set of stand-ins can't be built (they don't hash alike): its items in braces read the same.
    return _Shown(f"{{{', '.join(_safe(repr, item) for item in items)}}}") if items else value


class Blocked(BaseException):
    """Raised by a guard where a repr() would do what a capture must not (run a query).

    Not an Exception: reprlib catches those and shows `<Book instance at 0x...>`, and so may the app's own __repr__.
    """


def _safe(show: Callable[[object], str], value: object) -> str:
    try:
        return show(value)
    except Blocked:
        return f"<{type(value).__name__}: not shown, it would query the database>"
    except Exception:  # noqa: BLE001 (a broken __repr__ must not break the capture)
        return f"<unrepresentable {type(value).__name__}>"


def _quoted(text: str) -> str:
    """A string as Python can read it, in double quotes: "django.db.backends.postgresql", as a reader writes one."""
    return json.dumps(text, ensure_ascii=False)


class _DoubleQuoted(reprlib.Repr):
    """reprlib's limits, its strings in double quotes: settings and a template's context read as they are written."""

    @override
    def repr_str(self, x: str, level: int) -> str:
        del level
        shown = _quoted(x[: self.maxstring])
        if len(shown) > self.maxstring:
            head = max(0, (self.maxstring - 3) // 2)
            tail = max(0, self.maxstring - 3 - head)
            shown = _quoted(x[:head] + x[len(x) - tail :])
            shown = shown[:head] + self.fillvalue + shown[len(shown) - tail :]
        return shown


def _limited(limit: int, *, items: int, level: int, longest: int) -> reprlib.Repr:
    shown = _DoubleQuoted()
    shown.maxstring = shown.maxother = limit
    shown.maxlist = shown.maxtuple = shown.maxset = shown.maxfrozenset = shown.maxdeque = items
    shown.maxdict = min(items, 50)
    shown.maxlevel = level
    shown.maxlong = longest
    return shown


# Settings read whole more often than locals do: INSTALLED_APPS has dozens of apps, DATABASES a dozen keys.
_SETTINGS = _limited(500, items=100, level=6, longest=500)
SETTING_MAX = 4000


def setting_repr(name: str, value: object) -> str:
    """A setting as the console shows it: masked whole under a secret name, else its masked repr, cut."""
    if setting_sensitive(name):
        return MASK
    text = _safe(lambda shown: _SETTINGS.repr(masked(shown, setting_sensitive)), value)
    return text if len(text) <= SETTING_MAX else f"{text[: SETTING_MAX - 3]}..."


def settings_reprs(values: Mapping[str, object]) -> dict[str, str]:
    """Every UPPERCASE setting, by name, as setting_repr() shows it."""
    return guarded(lambda: {name: setting_repr(name, values[name]) for name in sorted(values) if name.isupper()})


# A template's context: the locals' limits (tracepanel.core.frames).
_CONTEXT = _limited(160, items=10, level=3, longest=40)
MAX_CONTEXT = 40
# The contexts one trace keeps: a template included in a loop of thousands of rows renders that many times, and each
# render's context would cost its repr()s. The first renders of each template show what it got, the loop's variable
# included.
MAX_CONTEXTS = 100
MAX_CONTEXTS_PER_TEMPLATE = 3


def context_wanted(trace: Trace | None, template: str) -> bool:
    """Whether this render of `template` gets its context: the setting is on, the trace's budget not spent."""
    if trace is None or not conf.get_config().capture_template_context:
        return False
    seen = trace.contexts.get(template, 0)
    if seen >= MAX_CONTEXTS_PER_TEMPLATE or sum(trace.contexts.values()) >= MAX_CONTEXTS:
        return False
    trace.contexts[template] = seen + 1
    return True


def _context_repr(value: object) -> str:
    return _CONTEXT.repr(masked(value, local_sensitive))


def context_attrs(items: Mapping[str, object]) -> Attrs:
    """`template.context.*`: its top-level names, their masked repr()s, how many were left out past MAX_CONTEXT."""
    names = [name for name in items if not name.startswith("__")]
    kept = names[:MAX_CONTEXT]
    shown = guarded(lambda: [MASK if local_sensitive(name) else _safe(_context_repr, items[name]) for name in kept])
    attrs: Attrs = {"template.context.names": list[Scalar](kept), "template.context.values": list[Scalar](shown)}
    if len(names) > len(kept):
        attrs["template.context.omitted"] = len(names) - len(kept)
    return attrs
