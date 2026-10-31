"""python-devtools: each `debug(...)` call is also a "dump" log record of the current trace, on the current span.

devtools prints as it always does, and debug() returns what it always returns: tracepanel only reads the DebugOutput
devtools builds for the call (Debug._process, which debug() and debug.format() both go through) and records it when
debug() asked for it, not format(). The record (logger `devtools`, level `debug`) has devtools' text as its message,
plain, and each argument in `dump.args`: its expression or keyword as devtools shows it, devtools' pretty text of its
value (cut at MAX_REPR), its type and its length. An argument whose expression or keyword names a secret (password,
token, api_key) is masked, in its text and in the message. `with debug.timer("name"):` times its block as a `function`
span named after the timer.

devtools' internals are not its API: anything unexpected about them leaves the call as devtools makes it, unrecorded,
and says so once.
"""

import importlib
import re
import sys
import time
from types import FrameType
from typing import Any

from tracepanel.core.context import current_span, current_trace, start_span
from tracepanel.core.frames import relative
from tracepanel.core.masking import MASK, counted, sensitive
from tracepanel.core.model import Attrs, AttrValue, Log
from tracepanel.core.patching import patch, patched
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


def _dump(argument: Any) -> tuple[dict[str, AttrValue], str]:  # noqa: ANN401 (a DebugArgument)
    """The argument's entry in dump.args, and its lines in the message (devtools' own, masked when secret)."""
    from devtools.debug import pformat  # noqa: PLC0415 (devtools is imported: debug() is running)

    value = argument.value
    expr = _expr(argument)
    kind = value.__class__.__name__
    if _secret(argument, expr):
        return {"expr": expr, "repr": MASK, "type": kind, "len": None}, f"{expr}: {MASK} ({kind})"
    try:
        text = pformat(value, highlight=False)
    except Exception:  # noqa: BLE001 (devtools falls back to repr() too)
        text = repr(value)
    length = next((v for k, v in argument.extra if k == "len"), None)
    entry: dict[str, AttrValue] = {
        "expr": expr,
        "repr": _capped(_plain(text)),
        "type": kind,
        "len": length if isinstance(length, int) else None,
    }
    return entry, _plain(argument.str(highlight=False))


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
