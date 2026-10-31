"""python-devtools: each `debug(...)` call is also a "dump" log record of the current trace, on the current span.

devtools prints as it always does, and debug() returns what it always returns: tracepanel only reads the DebugOutput
devtools builds for the call (Debug._process, which debug() and debug.format() both go through) and records it when
debug() asked for it, not format(). The record (logger `devtools`, level `debug`) has devtools' text as its message,
plain, and each argument in `dump.args`: its expression or keyword as devtools shows it, devtools' pretty text of its
value (cut at MAX_REPR), its type and its length. An argument whose expression or keyword names a secret (password,
token, api_key) is masked, in its text and in the message. Inside a value, what goes under a secret's name is masked
too: a dict's key, a dataclass', attrs class' or pydantic model's field, an object's attribute, a namedtuple's field
(masking.sensitive(), a card's number as masking.card_number() says), and a URL's credentials. devtools formats a
masked copy (the value itself is never changed); what the copy can't reach (an object it can't copy, an attribute a
descriptor holds, an ORM's column) is masked in the text, where it reads `name=value` or `'name': value`. The terminal
gets devtools' own text, unmasked: it is the developer's own screen, and a recorded trace can go further.
`with debug.timer("name"):` times its block as a `function` span named after the timer.

devtools' internals are not its API: anything unexpected about them leaves the call as devtools makes it, unrecorded,
and says so once.
"""

import copy
import dataclasses
import importlib
import re
import sys
import time
from types import FrameType, MemberDescriptorType, ModuleType
from typing import Any

from tracepanel.core.context import current_span, current_trace, start_span
from tracepanel.core.frames import relative
from tracepanel.core.masking import MASK, card_fields, card_number, counted, sensitive
from tracepanel.core.model import Attrs, AttrValue, Log
from tracepanel.core.patching import patch, patched
from tracepanel.core.values import mask_url
from tracepanel.integrations.logging import TracepanelHandler, keep

LOGGER = "devtools"
# How much of one value's text is kept, in characters; a longer one is cut there and marked.
MAX_REPR = 16 * 1024
CUT = "… (truncated)"
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_warned = False


def _warn_once(exc: BaseException) -> None:
    global _warned  # noqa: PLW0603 (said once per process)
    if not _warned:
        _warned = True
        sys.stderr.write(f"tracepanel: could not record a devtools debug() call: {exc!r}\n")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _capped(text: str) -> str:
    return text if len(text) <= MAX_REPR else text[:MAX_REPR] + CUT


def _expr(argument: Any) -> str | None:  # noqa: ANN401 (a DebugArgument)
    """The expression or keyword devtools shows before the value: none for a literal (debug("here"))."""
    from devtools.utils import is_literal  # noqa: PLC0415 (devtools is imported: debug() is running)

    name = argument.name
    return str(name) if name and not is_literal(name) else None


def _secret(argument: Any, expr: str | None) -> bool:  # noqa: ANN401 (a DebugArgument)
    # A keyword's value may come from a variable named after a secret: debug(value=password).
    names = [expr, *(value for key, value in argument.extra if key == "variable")]
    return any(isinstance(name, str) and sensitive(name) and not counted(name, argument.value) for name in names)


class _Masked:
    """MASK where a secret was, in the masked copy devtools formats: it reads as the mask, unquoted."""

    __slots__ = ()

    def __repr__(self) -> str:
        return MASK


_MASKED = _Masked()
# How deep the masked copy goes: past it, the value is formatted as it is (the text's names are still masked).
_MAX_DEPTH = 8


def _hidden(name: object, value: object, parent: object, *, beside_card: bool) -> bool:
    """Whether the value under `name` is a secret: by its name, or as a card's number."""
    if isinstance(name, str) and sensitive(name) and not counted(name, value):
        return True
    return card_number(name, value, parent, beside_card=beside_card)


def _masked(value: object, depth: int = 0, parent: object = None, seen: frozenset[int] = frozenset()) -> object:
    """`value` with its secrets masked, as devtools formats it: the value itself when it holds none, else a copy.

    The copy is of the same kind, so devtools shows it the same way: a dict (its subclass too), a list, tuple,
    namedtuple or set, a dataclass, an attrs class, a pydantic model, an object whose own repr() shows its attributes.
    """
    if isinstance(value, str):
        return mask_url(value)
    if depth >= _MAX_DEPTH or id(value) in seen or isinstance(value, (int, float, bytes, bytearray, type)):
        return value
    seen |= {id(value)}
    try:
        if isinstance(value, dict):
            return _masked_dict(value, depth, parent, seen)
        if isinstance(value, (list, tuple, set, frozenset)):
            return _masked_items(value, depth, parent, seen)
        return _masked_object(value, depth, seen)
    # A value that can't be copied, or made again of its masked items, is formatted as it is (the text is masked).
    except Exception:  # noqa: BLE001
        return value


def _masked_dict(value: dict[Any, Any], depth: int, parent: object, seen: frozenset[int]) -> object:
    beside_card = card_fields(value)
    changed = {
        key: _MASKED if _hidden(key, item, parent, beside_card=beside_card) else _masked(item, depth + 1, key, seen)
        for key, item in value.items()
    }
    changed = {key: item for key, item in changed.items() if item is not value[key]}
    if not changed:
        return value
    if type(value) is dict:
        return {key: changed.get(key, item) for key, item in value.items()}
    # OrderedDict, defaultdict, Counter: devtools names their kind. Set on the copy as a dict, past their own hooks.
    masked = copy.copy(value)
    for key, item in changed.items():
        dict.__setitem__(masked, key, item)
    return masked


def _masked_items(
    value: list[object] | tuple[object, ...] | set[object] | frozenset[object],
    depth: int,
    parent: object,
    seen: frozenset[int],
) -> object:
    fields = getattr(value, "_fields", None)
    if isinstance(value, tuple) and isinstance(fields, tuple):
        # A namedtuple: its fields are named.
        beside_card = card_fields(fields)
        items = [
            _MASKED if _hidden(name, item, parent, beside_card=beside_card) else _masked(item, depth + 1, name, seen)
            for name, item in zip(fields, value, strict=False)
        ]
    else:
        items = [_masked(item, depth + 1, parent, seen) for item in value]
    if all(item is original for item, original in zip(items, value, strict=True)):
        return value
    if isinstance(value, tuple) and isinstance(fields, tuple):
        return type(value)._make(items)  # type: ignore[attr-defined]
    if isinstance(value, (set, frozenset)):
        # Stand-ins don't hash alike: the items in braces read the same.
        return _Shown("{" + ", ".join(repr(item) for item in items) + "}")
    return type(value)(items)


class _Shown:
    """A stand-in whose repr() is given."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _fields(value: object) -> list[str] | None:
    """The names of what devtools shows of an object: its fields or attributes; None if it shows none of them."""
    kind = type(value)
    if dataclasses.is_dataclass(value):
        return [field.name for field in dataclasses.fields(value)]
    attrs_fields = getattr(kind, "__attrs_attrs__", None)
    if attrs_fields is not None:
        return [field.name for field in attrs_fields]
    if hasattr(value, "__pretty__") and hasattr(value, "__dict__"):
        # A pydantic model (devtools asks it how it looks): its fields are in its __dict__.
        return list(vars(value))
    if (
        kind.__repr__ is not object.__repr__
        and kind.__module__ != "builtins"
        and not isinstance(value, ModuleType)
        and hasattr(value, "__dict__")
    ):
        # Its own repr() may show what its attributes hold.
        return list(vars(value))
    return None


def _masked_object(value: object, depth: int, seen: frozenset[int]) -> object:
    names = _fields(value)
    if not names:
        return value
    kind = type(value)
    current = {name: getattr(value, name, None) for name in names}
    beside_card = card_fields(names)
    changed: dict[str, object] = {}
    for name, item in current.items():
        masked = _MASKED if _hidden(name, item, None, beside_card=beside_card) else _masked(item, depth + 1, name, seen)
        if masked is not item:
            changed[name] = masked
    if not changed:
        return value
    holder = getattr(value, "__dict__", None)
    copied = copy.copy(value)
    for name, item in changed.items():
        held_by = getattr(kind, name, None)
        if isinstance(held_by, MemberDescriptorType):
            # A slot: the copy's own.
            object.__setattr__(copied, name, item)
        elif isinstance(holder, dict) and name in holder and not hasattr(held_by, "__set__"):
            # In the copy's __dict__, past its __setattr__ (a frozen dataclass, a validating model).
            vars(copied)[name] = item
        # Otherwise a descriptor holds it (an ORM's column, whose state the copy shares): the text is masked.
    return copied


# `name=value` (a field, a keyword) and `'name': value` (a dict's key) in formatted text: a value quoted, or up to the
# next separator. What the masked copy couldn't reach.
_VALUE = r"""(?P<value>[bBrRuU]{0,2}'(?:[^'\\\n]|\\.)*'|[bBrRuU]{0,2}"(?:[^"\\\n]|\\.)*"|[^\s,)}\]&;'"]+)"""
_PAIR = re.compile(
    r"""(?P<head>(?P<quote>['"])(?P<key>[^'"\n]{1,100})(?P=quote):\s*|(?<![\w.])(?P<name>[A-Za-z_]\w{0,99})=)"""
    + _VALUE
)


def _masked_text(text: str) -> str:
    """The text with each `name=value` and `'name': value` under a secret's name masked."""

    def mask(match: re.Match[str]) -> str:
        name, value = match["key"] or match["name"], match["value"]
        if value == MASK or not sensitive(name) or (value.isdigit() and counted(name, int(value))):
            return match[0]
        return match["head"] + MASK

    return _PAIR.sub(mask, text) if ("=" in text or ":" in text) else text


def _dump(argument: Any) -> tuple[dict[str, AttrValue], str]:  # noqa: ANN401 (a DebugArgument)
    """The argument's entry in dump.args, and its lines in the message (devtools' own, its secrets masked)."""
    from devtools.debug import DebugArgument, pformat  # noqa: PLC0415 (devtools is imported: debug() is running)

    value = argument.value
    expr = _expr(argument)
    kind = value.__class__.__name__
    if _secret(argument, expr):
        return {"expr": expr, "repr": MASK, "type": kind, "len": None}, f"{expr}: {MASK} ({kind})"
    masked = _masked(value)
    try:
        text = pformat(masked, highlight=False)
    except Exception:  # noqa: BLE001 (devtools falls back to repr() too)
        text = repr(masked)
    if masked is not value:
        # devtools' lines for the copy, with the value's own suffix: its type and length.
        shown = DebugArgument(masked, name=argument.name)
        shown.extra = argument.extra
        lines = shown.str(highlight=False).replace(f"({type(masked).__name__})", f"({kind})", 1)
    else:
        lines = argument.str(highlight=False)
    length = next((v for k, v in argument.extra if k == "len"), None)
    entry: dict[str, AttrValue] = {
        "expr": expr,
        "repr": _capped(_masked_text(_plain(text))),
        "type": kind,
        "len": length if isinstance(length, int) else None,
    }
    return entry, _masked_text(_plain(lines))


def _record(output: Any, frame: FrameType) -> None:  # noqa: ANN401 (a DebugOutput)
    trace = current_trace()
    if trace is None or TracepanelHandler.ignored(LOGGER):
        return
    dumps = [_dump(argument) for argument in output.arguments]
    head = f"{output.filename}:{output.lineno} {output.frame}"
    if output.warning:
        head += f" ({output.warning})"
    code = frame.f_code
    attrs: Attrs = {
        "log.kind": "dump",
        # A list of objects, which the trace's other attrs never hold: the agent keeps attrs as JSON.
        "dump.args": [entry for entry, _ in dumps],  # type: ignore[dict-item]
        "code.origin": f"{relative(code.co_filename)}:{frame.f_lineno} in {code.co_name}",
    }
    span = current_span()
    keep(
        trace,
        Log(
            span_id=span.id if span else None,
            time=int(time.time() * 1_000_000),
            level="debug",
            logger=LOGGER,
            message="\n    ".join([head, *(lines for _, lines in dumps)]),
            attrs=attrs,
        ),
    )


def _patch_process(debug_class: type) -> None:
    original = debug_class._process  # noqa: SLF001

    def _process(self: Any, args: Any, kwargs: Any, frame_depth: int) -> Any:  # noqa: ANN401 (devtools' own)
        # This wrapper is one more frame between devtools and the caller it looks for.
        output = original(self, args, kwargs, frame_depth + 1)
        if current_trace() is None:
            return output
        try:
            caller = sys._getframe(1)  # noqa: SLF001
            # debug() prints what it asked for; debug.format() hands it back: not a dump.
            if caller.f_code.co_name == "__call__" and caller.f_globals.get("__name__") == "devtools.debug":
                _record(output, sys._getframe(frame_depth))  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 (a debug() call must never break)
            _warn_once(exc)
        return output

    patch(debug_class, "_process", _process)


def _patch_timer(timer_class: type) -> None:
    original_enter, original_exit = timer_class.__enter__, timer_class.__exit__

    def __enter__(self: Any) -> Any:  # noqa: ANN401, N807 (devtools' Timer)
        entered = original_enter(self)
        if current_trace() is not None:
            try:
                block = start_span("function", str(getattr(self, "_name", None) or "debug.timer"))
                block.__enter__()
                self.__dict__.setdefault("_tracepanel_spans", []).append(block)
            except Exception as exc:  # noqa: BLE001 (a timer must never break)
                _warn_once(exc)
        return entered

    def __exit__(self: Any, *args: Any) -> None:  # noqa: ANN401, N807 (devtools' Timer)
        try:
            spans = self.__dict__.get("_tracepanel_spans")
            if spans:
                spans.pop().__exit__(*args)
        except Exception as exc:  # noqa: BLE001 (a timer must never break)
            _warn_once(exc)
        original_exit(self, *args)

    patch(timer_class, "__enter__", __enter__)
    patch(timer_class, "__exit__", __exit__)


def install() -> None:
    # The package's `debug` is the Debug instance, which hides the module of the same name.
    debug_module = importlib.import_module("devtools.debug")
    timer_module = importlib.import_module("devtools.timer")
    debug_class = getattr(debug_module, "Debug", None)
    if isinstance(debug_class, type) and callable(getattr(debug_class, "_process", None)):
        if not patched(debug_class, "_process"):
            _patch_process(debug_class)
    else:
        _warn_once(AttributeError("devtools.debug.Debug._process"))
    timer_class = getattr(timer_module, "Timer", None)
    if isinstance(timer_class, type) and hasattr(timer_class, "__exit__") and not patched(timer_class, "__enter__"):
        _patch_timer(timer_class)
