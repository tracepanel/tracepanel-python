"""Stack inspection: project code vs. the standard library and installed packages."""

import functools
import linecache
import os
import re
import reprlib
import sys
import sysconfig
from contextvars import ContextVar
from pathlib import Path
from types import FrameType, TracebackType

from tracepanel.core import conf
from tracepanel.core.model import Frame

_LIBRARY_PATHS = tuple({str(Path(sysconfig.get_paths()[key]).resolve()) for key in ("stdlib", "purelib")})
_SKIP_MODULES = ("tracepanel", "django", "asgiref")

CONTEXT_LINES = 5
MAX_FRAMES = 50
MAX_LOCALS = 40
SENSITIVE = re.compile(
    r"pass(word|wd)?|secret|token|api_?key|auth|cookie|session|csrf|signature|private", re.IGNORECASE
)
MASK = "********"

_repr = reprlib.Repr()
_repr.maxstring = 160
_repr.maxother = 160
_repr.maxlist = _repr.maxtuple = _repr.maxset = _repr.maxdict = 10
_repr.maxlevel = 3


def is_in_app(filename: str, module: str) -> bool:
    if module.split(".", 1)[0] in _SKIP_MODULES:
        return False
    return _project_file(filename)


@functools.cache
def _project_file(filename: str) -> bool:
    """Project code, not the standard library or an installed package; cached: resolving touches the disk."""
    if filename.startswith("<"):
        return False
    path = str(Path(filename).resolve())
    return not path.startswith(_LIBRARY_PATHS) and "site-packages" not in path


def relative(filename: str) -> str:
    return _relative(filename, conf.get_config().root)


@functools.lru_cache(maxsize=4096)
def _relative(filename: str, root: str) -> str:
    try:
        rel = os.path.relpath(filename, root)
    except ValueError:
        return filename
    return filename if rel.startswith("..") else rel


# Where project code awaited sync_to_async(): the async ORM runs queries in a thread whose stack has no project
# frames, so the place is taken before the hop (see tracepanel.aio) and travels with the context.
awaited_from: ContextVar[str | None] = ContextVar("tracepanel_awaited_from", default=None)


def origin(skip: int = 2) -> str | None:
    """`path:line in function` of the innermost project frame calling into the current code."""
    frame: FrameType | None = sys._getframe(skip)  # noqa: SLF001
    while frame is not None:
        code = frame.f_code
        module = frame.f_globals.get("__name__", "")
        if is_in_app(code.co_filename, module):
            return f"{relative(code.co_filename)}:{frame.f_lineno} in {code.co_name}"
        if module == "asgiref.sync" and awaited_from.get() is not None:
            # The thread sync_to_async() runs in: what lies further up belongs to whoever waits, not to this call.
            return awaited_from.get()
        frame = frame.f_back
    return awaited_from.get()


def safe_repr(name: str, value: object) -> str:
    if SENSITIVE.search(name):
        return MASK
    try:
        return _repr.repr(value)
    except Exception:  # noqa: BLE001 (a broken __repr__ must not break error capture)
        return f"<unrepresentable {type(value).__name__}>"


def _locals(frame: FrameType) -> dict[str, str]:
    items = [(name, value) for name, value in frame.f_locals.items() if not name.startswith("__")]
    return {name: safe_repr(name, value) for name, value in items[:MAX_LOCALS]}


def _context(filename: str, lineno: int, module_globals: dict[str, object]) -> tuple[int | None, list[str]]:
    lines = linecache.getlines(filename, module_globals)
    if not lines:
        return None, []
    start = max(lineno - CONTEXT_LINES, 1)
    return start, [line.rstrip("\n") for line in lines[start - 1 : lineno + CONTEXT_LINES]]


def from_traceback(tb: TracebackType | None, *, with_locals: bool = False) -> list[Frame]:
    frames: list[Frame] = []
    while tb is not None:
        frame = tb.tb_frame
        code = frame.f_code
        module = frame.f_globals.get("__name__", "")
        in_app = is_in_app(code.co_filename, module)
        context_start, context = _context(code.co_filename, tb.tb_lineno, frame.f_globals)
        frames.append(
            Frame(
                filename=relative(code.co_filename),
                module=module,
                function=code.co_name,
                lineno=tb.tb_lineno,
                in_app=in_app,
                context_start=context_start,
                context=context,
                locals=_locals(frame) if with_locals and in_app else None,
            )
        )
        tb = tb.tb_next
    return frames[-MAX_FRAMES:]
