"""Stack inspection: project code vs. the standard library and installed packages."""

import functools
import linecache
import os
import re
import reprlib
import sys
import sysconfig
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from types import CodeType, FrameType, TracebackType

from tracepanel.core import conf
from tracepanel.core.model import Frame

# The standard library, installed packages, and the environment's scripts (.venv/bin/uvicorn, the entry point a
# server's stack starts in): none of them is the project's code, even inside the project's directory.
_LIBRARY_PATHS = tuple(
    {
        str(Path(sysconfig.get_paths()[key]).resolve())
        for key in ("stdlib", "platstdlib", "purelib", "platlib", "scripts")
    }
)
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


@functools.lru_cache(maxsize=8192)
def is_in_app(filename: str, module: str) -> bool:
    """Project code; cached, as every SQL statement walks the stack frame by frame."""
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


@functools.lru_cache(maxsize=4096)
def library_path(filename: str) -> str:
    """A library's or Python's file as its package names it (django/core/handlers/base.py), not its install path."""
    for root in sorted(_LIBRARY_PATHS, key=len, reverse=True):
        if filename.startswith(root + os.sep):
            return filename[len(root) + 1 :]
    marker = f"{os.sep}site-packages{os.sep}"
    return filename.split(marker, 1)[1] if marker in filename else filename


# Where project code awaited sync_to_async(): the async ORM runs queries in a thread whose stack has no project
# frames, so the place is taken before the hop (see tracepanel.aio) and travels with the context.
awaited_from: ContextVar[str | None] = ContextVar("tracepanel_awaited_from", default=None)


# A Jinja template's compiled code keeps the template in its globals; it maps the code's lines to the template's.
JINJA_TEMPLATE = "__jinja_template__"


# Code object -> whether it is project code. A code object belongs to one module for good: one dict lookup per frame
# instead of reading the frame's globals. Bounded: code made at runtime (exec, templates) would grow it forever.
_IN_APP_BY_CODE: dict[CodeType, bool] = {}
_IN_APP_BY_CODE_MAX = 20_000


def _code_in_app(frame: FrameType) -> bool:
    code = frame.f_code
    known = _IN_APP_BY_CODE.get(code)
    if known is None:
        if len(_IN_APP_BY_CODE) >= _IN_APP_BY_CODE_MAX:
            _IN_APP_BY_CODE.clear()
        # A Jinja template's compiled code runs from the template's file: the template, not the project's Python.
        known = _IN_APP_BY_CODE[code] = JINJA_TEMPLATE not in frame.f_globals and is_in_app(
            code.co_filename, frame.f_globals.get("__name__", "")
        )
    return known


# The template and its line at a frame of a template engine's own, or None: (name or None, line).
TemplatePlace = Callable[[FrameType], tuple[str | None, int] | None]
# Engines that render in Python register the code object their nodes render in (Django's Node.render_annotated).
_TEMPLATE_CODES: dict[CodeType, TemplatePlace] = {}


def template_frames(code: CodeType, place: TemplatePlace) -> None:
    """Frames running `code` are a template engine's rendering: `place` reads the template and its line off one."""
    _TEMPLATE_CODES[code] = place


def _jinja_place(template: object, frame: FrameType) -> tuple[str | None, int] | None:
    lineno = getattr(template, "get_corresponding_lineno", None)
    if lineno is None:
        return None
    name = getattr(template, "name", None)
    return (str(name) if name else None), int(lineno(frame.f_lineno))


def template_place(skip: int = 2) -> tuple[str | None, int] | None:
    """The template and line rendering at the current point: the innermost Jinja template, or registered engine node.

    The name is None where the template has none (one made from a string): its span's name stands for it.
    """
    frame: FrameType | None = sys._getframe(skip)  # noqa: SLF001
    while frame is not None:
        jinja = frame.f_globals.get(JINJA_TEMPLATE)
        place = _jinja_place(jinja, frame) if jinja is not None else None
        if place is None and (reader := _TEMPLATE_CODES.get(frame.f_code)) is not None:
            place = reader(frame)
        if place is not None:
            return place
        frame = frame.f_back
    return None


def origin(skip: int = 2) -> str | None:
    """`path:line in function` of the innermost project frame calling into the current code."""
    frame: FrameType | None = sys._getframe(skip)  # noqa: SLF001
    while frame is not None:
        if _code_in_app(frame):
            code = frame.f_code
            return f"{relative(code.co_filename)}:{frame.f_lineno} in {code.co_name}"
        if awaited_from.get() is not None and frame.f_globals.get("__name__") == "asgiref.sync":
            # The thread sync_to_async() runs in: what lies further up belongs to whoever waits, not to this call.
            return awaited_from.get()
        frame = frame.f_back
    return awaited_from.get()


# What makes a query rather than asks for one: the ORM and drivers, the helpers around their calls, Tracepanel itself.
# Past these is the library code that wanted the rows (Django's admin, a form, a template tag).
_QUERY_LAYERS = (
    "tracepanel",
    "django.db",
    "django.utils.functional",
    "django.utils.asyncio",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "sqlite3",
    "asyncpg",
    "pymysql",
    "MySQLdb",
    "oracledb",
    "asgiref",
    "contextlib",
    "functools",
    "threading",
    "concurrent",
)


@functools.lru_cache(maxsize=4096)
def _in_query_layer(module: str) -> bool:
    return any(module == layer or module.startswith(f"{layer}.") for layer in _QUERY_LAYERS)


def library_caller(skip: int = 2) -> str | None:
    """`path:line in function` of the library code that asked for a query no project code made.

    The first frame past the ORM and the driver: django/contrib/admin/views/main.py:… in get_results.
    """
    frame: FrameType | None = sys._getframe(skip)  # noqa: SLF001
    while frame is not None:
        if not _in_query_layer(str(frame.f_globals.get("__name__", ""))):
            code = frame.f_code
            return f"{library_path(code.co_filename)}:{frame.f_lineno} in {code.co_qualname}"
        frame = frame.f_back
    return None


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
