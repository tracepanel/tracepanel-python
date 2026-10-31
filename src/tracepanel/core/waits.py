"""Which frames of a profile wait rather than compute: the event loop's select, a sleep, a socket read, a lock.

A sample whose innermost frame waits is time the trace spent waiting. Two kinds of frame wait:

- A Python function of the standard library that only waits (`threading.Event.wait`, `selectors.EpollSelector.select`,
  `asyncio.sleep`, …): known by its module and name (`waits()`).
- A call into C that waits, made from the innermost Python frame (`time.sleep(1.5)`, `sock.recv(4096)`,
  `os.read(fd, n)`): no Python frame stands for it, so the sampler adds one (`calling()`). The call is read from the
  frame's current instruction and its source text, the function looked up in the frame's globals. A method of an
  object (a socket, a lock) is known by its name only: the sampler doesn't read another thread's locals.
"""

import builtins
import contextlib
import dis
import linecache
import os
import re
import select
import time
from types import CodeType, FrameType, ModuleType

# (module, qualified name) of the standard library's Python functions that wait.
WAITING = frozenset(
    {
        ("asyncio.tasks", "sleep"),
        ("asyncio.tasks", "__sleep0"),
        ("threading", "Condition.wait"),
        ("threading", "Condition.wait_for"),
        ("threading", "Event.wait"),
        ("threading", "Semaphore.acquire"),
        ("threading", "Barrier.wait"),
        ("threading", "Thread.join"),
        ("threading", "Thread._wait_for_tstate_lock"),
        ("queue", "Queue.get"),
        ("queue", "Queue.put"),
        ("queue", "Queue.join"),
        ("subprocess", "Popen.wait"),
        ("subprocess", "Popen._wait"),
        ("subprocess", "Popen._try_wait"),
        ("subprocess", "Popen.communicate"),
        ("subprocess", "Popen._communicate"),
        ("socket", "socket.accept"),
        ("socket", "create_connection"),
        ("socket", "SocketIO.readinto"),
        ("ssl", "SSLSocket.recv"),
        ("ssl", "SSLSocket.recv_into"),
        ("ssl", "SSLSocket.read"),
        ("ssl", "SSLSocket.do_handshake"),
        ("ssl", "SSLSocket.accept"),
        ("ssl", "SSLObject.read"),
        ("concurrent.futures._base", "Future.result"),
        ("concurrent.futures._base", "wait"),
    }
)
# Methods of a local object that wait in C, known by name (`sock.recv(…)`, `lock.acquire()`, `poller.poll()`).
WAITING_METHODS = frozenset(
    {"recv", "recv_into", "recvfrom", "recvfrom_into", "recvmsg", "accept", "connect", "acquire", "poll", "select"}
)
# Calls whose source text names a function: `time.sleep(`, `os.read(`, `sock.recv(`.
_CALL = re.compile(r"^\s*(?:await\s+)?([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*\(")
# What a call's instruction is read as, by code and offset: the dotted name it calls, or None. Kept small.
_CALLS: dict[tuple[CodeType, int], tuple[str, ...] | None] = {}
MAX_CALLS = 10_000


def waits(module: str | None, qualname: str) -> bool:
    """Whether a Python function of the standard library only waits: the event loop's select, a sleep, a lock."""
    if module == "selectors":
        return qualname == "select" or qualname.endswith(".select")
    return (module or "", qualname) in WAITING


def _c_waits() -> dict[int, str]:
    """The C functions that wait, by id: what a frame's globals may call them by."""
    found: dict[int, str] = {}
    for module, names in ((time, ("sleep",)), (select, ("select",)), (os, ("read", "readv", "wait", "waitpid"))):
        for name in names:
            function = getattr(module, name, None)
            if function is not None:
                found[id(function)] = f"{module.__name__}.{name}"
    return found


_C_WAITS = _c_waits()


def calling(frame: FrameType) -> str | None:
    """The C call that waits, which the innermost Python frame is making now: `time.sleep`, `sock.recv`; else None."""
    code, offset = frame.f_code, frame.f_lasti
    key = (code, offset)
    path = _CALLS.get(key, ())
    if path == ():
        path = _call_path(code, offset)
        if len(_CALLS) >= MAX_CALLS:
            _CALLS.clear()
        _CALLS[key] = path
    if not path:
        return None
    if len(path) > 1 and path[-1] in WAITING_METHODS and not isinstance(frame.f_globals.get(path[0]), ModuleType):
        # A method of an object, not a module's function: a socket's, a lock's, a poller's.
        return ".".join(path[-2:])
    return _resolved(frame, path)


def _resolved(frame: FrameType, path: tuple[str, ...]) -> str | None:
    """The function a dotted name stands for in the frame's globals, if it is a C function that waits.

    Only modules are looked into: an attribute of anything else could run code.
    """
    target: object = frame.f_globals.get(path[0], getattr(builtins, path[0], None))
    for name in path[1:]:
        if not isinstance(target, ModuleType):
            return None
        target = getattr(target, name, None)
    return _C_WAITS.get(id(target))


# The instructions a frame is at while it calls. Python 3.11's call is PRECALL then CALL, and once the code is warm a
# specialized PRECALL makes a C function's call itself, without the CALL: the frame waits at the PRECALL.
_CALLING = ("CALL", "PRECALL")


def _call_path(code: CodeType, offset: int) -> tuple[str, ...] | None:
    """The dotted name a call instruction calls, from its source text: None for anything but a call by name."""
    if offset < 0 or offset >= len(code.co_code) or not dis.opname[code.co_code[offset]].startswith(_CALLING):
        return None
    with contextlib.suppress(IndexError, ValueError, TypeError):
        line, _, column, _ = list(code.co_positions())[offset // 2]
        if line is None or column is None:
            return None
        text = linecache.getline(code.co_filename, line).encode()[column:].decode(errors="replace")
        match = _CALL.match(text)
        if match is not None:
            return tuple(part.strip() for part in match[1].split("."))
    return None
