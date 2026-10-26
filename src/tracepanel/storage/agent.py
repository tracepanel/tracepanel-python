"""Sending traces to tracepanel-agent, which keeps the project's traces, finds their issues and serves the console.

The agent leaves its address in `<store dir>/agent.addr`; the first trace starts one when none answers (see start()).
A process only serializes its traces and sends them in batches. Without an agent (none for this platform, or it
couldn't start) traces aren't kept: the terminal says why once, and the process looks for one again a few seconds
later.

The wire: a hello line, `{"protocol": 1, "max_traces": 2000, "thresholds": {...}, "service": "shop", "capabilities":
[...], "console": [...], "app": {...}}` (what the app offers the console: the one place it says so; the agent alone
answers the console's `api/capabilities` from it; `app` carries the console's App tab: the app's settings,
masked, and the versions it runs on, `{"settings": {"DEBUG": "True", ...}, "versions": [{"name": "Python", "version":
"3.13.1", "kind": "runtime"}, ...]}`, and what its checks found once they ran, `"checks"` and `"migrations"`, see
tracepanel.core.appinfo), answered by `{"ok": true}` (or a refusal with its reason); then frames, a 4-byte big-endian
length and a JSON array of `Trace.to_dict()`, or an object: task events, `{"tasks": [...]}`, or what the checks found
after the hello, `{"app": {"checks": [...], "migrations": {...}}}` (an agent from before ignores either).
"""

import contextlib
import json
import os
import platform
import select
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from tracepanel._agent import AGENT, find_agent_bin
from tracepanel._compat import override
from tracepanel.core import appinfo, conf, forks
from tracepanel.core.model import Trace
from tracepanel.storage import transport
from tracepanel.storage.transport import BatchWriter, Event

PROTOCOL = 1
ADDR_FILE = "agent.addr"
# Where the agent serves the console (with --serve): an app's /__tracepanel__/ sends its API calls there.
CONSOLE_FILE = "console.addr"
LOG_FILE = "agent.log"
# How an agent exits when another one already serves its store (it holds the store's lock): that one is waited for.
ALREADY_RUNS = 3
# What an app's processes take in turn to start the store's agent: one starts it, the others find it answering.
START_LOCK_FILE = "start.lock"
# An agent an SDK started exits after this long unused; how long the SDK waits for it to answer.
IDLE_EXIT_S = 1800
START_WAIT_S = 1.0
# How long to wait for the agent to answer.
CONNECT_TIMEOUT_S = 0.5
# A stuck agent must not hold the writer thread (and the queue behind it) for long.
SEND_TIMEOUT_S = 2.0
# Without an agent, how often to look for one again.
RETRY_S = 5.0
# How often to start an agent again: one that exited (idle, killed) is started anew, one that can't start isn't retried
# on every request.
RESTART_S = 30.0


def store_dir(config: conf.Config) -> Path:
    return conf.project_root() / config.dir


def options(config: conf.Config) -> list[str]:
    """The agent's options the settings ask for: the LLM prices file."""
    return ["--llm-prices", str(conf.project_root() / config.llm_prices)] if config.llm_prices else []


def binary() -> str | None:
    """The tracepanel-agent executable: $TRACEPANEL_AGENT_BIN, the installed one, or a sibling checkout's build."""
    if explicit := os.environ.get("TRACEPANEL_AGENT_BIN"):
        return explicit if Path(explicit).is_file() else None
    if found := find_agent_bin():
        return found
    # tracepanel-python used from its checkout (development): a tracepanel checkout next to it, its newest build.
    target = Path(__file__).resolve().parents[3].parent / "tracepanel" / "target"
    builds = [build for build in (target / "release" / AGENT, target / "debug" / AGENT) if build.is_file()]
    return str(max(builds, key=lambda build: build.stat().st_mtime)) if builds else None


def can_start(config: conf.Config) -> bool:
    """Start an agent on demand: with the "auto" setting, and never under a test runner."""
    return config.agent == "auto" and "pytest" not in sys.modules


def start(config: conf.Config) -> subprocess.Popen[bytes] | None:
    """Start the project's agent, serving the console too; its own session, so an app's restarts don't stop it.

    It exits after IDLE_EXIT_S unused. Returns the process once it answers (writes its address); None if it didn't, or
    if another process's agent answers.
    """
    executable = binary()
    if executable is None:
        return None
    directory = store_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    # An app's workers, forked together, each start one: in turn.
    with _one_at_a_time(directory / START_LOCK_FILE):
        return None if answers(address(config)) else _spawn(executable, directory, config)


def _spawn(executable: str, directory: Path, config: conf.Config) -> subprocess.Popen[bytes] | None:
    # An agent that didn't exit cleanly (killed, a reboot) leaves its addresses behind: the next one removes them, while
    # it holds the store's lock. Not here: they may be another process's agent's, starting this very moment.
    command = [
        executable,
        "--dir",
        str(directory),
        "--serve",
        "127.0.0.1:0",
        "--idle-exit",
        str(IDLE_EXIT_S),
        *options(config),
    ]
    # Its own session (process group on Windows): Ctrl-C and reloads of the app don't reach it.
    windows = sys.platform == "win32"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with (directory / LOG_FILE).open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 (tracepanel's own agent)
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=not windows,
            creationflags=flags if windows else 0,
        )
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        exited = process.poll()
        if answers(address(config)):
            if exited is not None:
                # Another process's agent got there first (one agent per store): this process sends to it.
                return None
            sys.stderr.write(f"tracepanel ▸ agent started for {directory}\n")
            return process
        if exited is not None and exited != ALREADY_RUNS:
            break
        # Started, or another process's agent is: until it answers.
        time.sleep(0.02)
    _not_started(directory)
    return None


@contextlib.contextmanager
def _one_at_a_time(path: Path) -> Iterator[None]:
    """An exclusive lock on `path` while inside: the OS lets it go if the process dies holding it.

    Without one (a file system that has none), inside anyway: the agent's own lock keeps one per store.
    """
    with path.open("a+b") as file:
        locked = _lock(file)
        try:
            yield
        finally:
            if locked:
                _unlock(file)


if sys.platform == "win32":

    def _lock(file: BinaryIO) -> bool:
        file.seek(0)
        try:
            # Tries for 10 s.
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            return False
        return True

    def _unlock(file: BinaryIO) -> None:
        file.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

else:

    def _lock(file: BinaryIO) -> bool:
        try:
            fcntl.flock(file, fcntl.LOCK_EX)
        except OSError:
            return False
        return True

    def _unlock(file: BinaryIO) -> None:
        # Explicitly: a child forked meanwhile holds the same lock through its copy of the file.
        with contextlib.suppress(OSError):
            fcntl.flock(file, fcntl.LOCK_UN)


def _not_started(directory: Path) -> None:
    sys.stderr.write(f"tracepanel ▸ the agent didn't start (see {directory / LOG_FILE}): traces aren't kept\n")


def unavailable(config: conf.Config) -> str:
    """Why there is no agent, and what to do: the terminal's line, the console's page."""
    if config.agent == "off":
        return "the agent is off (the `agent` setting): traces aren't kept and there is no console."
    if binary() is None:
        return (
            f"no tracepanel-agent for this platform ({sys.platform}, {platform.machine()}): traces aren't kept and "
            "there is no console. Use an agent on another machine: TRACEPANEL_AGENT=tcp:HOST:PORT."
        )
    return f"the agent isn't running (see {store_dir(config) / LOG_FILE}): traces aren't kept until it is."


_start_lock = forks.lock()
_start_after = 0.0


def ensure_started(config: conf.Config) -> bool:
    """An agent for this store: one answering already, or one started now (every RESTART_S at most). True if one is."""
    global _start_after  # noqa: PLW0603 (one start at a time per process)
    if answers(address(config)) and console_url(config):
        return True
    if not can_start(config):
        return False
    with _start_lock:
        if time.monotonic() >= _start_after:
            _start_after = time.monotonic() + RESTART_S
            # Another process's agent may win the race (one per store): then its address is there.
            start(config)
    return answers(address(config))


def console_url(config: conf.Config) -> str | None:
    """`http://127.0.0.1:PORT/` of the agent's console, when an agent serves this store's.

    An agent named in the settings (`tcp:host.docker.internal:7701`) serves it at `console_url`.
    """
    if config.agent == "off":
        return None
    if config.agent != "auto":
        return config.console_url
    try:
        return (store_dir(config) / CONSOLE_FILE).read_text().strip() or None
    except OSError:
        return None


def address(config: conf.Config) -> str | None:
    """`unix:/path` of the agent: the `agent` setting, or with "auto" the file the agent writes."""
    if config.agent != "auto":
        return config.agent or None
    try:
        return (store_dir(config) / ADDR_FILE).read_text().strip() or None
    except OSError:
        return None


def answers(addr: str | None) -> bool:
    """Is an agent listening at `addr`? An address file alone may be a stopped agent's."""
    sock = _open(addr) if addr else None
    if sock is None:
        return False
    sock.close()
    return True


# What the app answers at its own console, under its prefix (EXPLAIN, Django's user switching), each with its check;
# the console integrations offer them (tracepanel.console.app), the hello tells the agent which hold.
CONSOLE: dict[str, Callable[[], bool]] = {}


def offer(name: str, check: Callable[[], bool]) -> None:
    """Say in the hello that the app's console answers `name` (`explain`, `users`) while `check()` holds."""
    CONSOLE[name] = check


# What the integrations send the agent beyond traces (`live_tasks`: Celery's task events), as they say on install.
SENDS: set[str] = set()


def sends(name: str) -> None:
    """Say in the hello that this app sends `name` (`live_tasks`)."""
    SENDS.add(name)


def capabilities(config: conf.Config) -> list[str]:
    """What this app's traces hold beyond the usual, and what it sends besides them.

    A profile when one is asked for, with its memory too (`profile_memory`), the request's details; task events
    (`live_tasks`).
    """
    offered = (
        ("profile", config.profile_enabled),
        ("profile_memory", config.profile_enabled),
        ("request", config.capture_request),
    )
    own = [name for name, on in offered if on]
    return own + sorted(SENDS)


def console_offers() -> list[str]:
    """What the app answers at its own console now, of what was offered."""
    return [name for name, check in CONSOLE.items() if check()]


def connect(  # noqa: PLR0913 (the hello's fields)
    addr: str,
    max_traces: int,
    thresholds: dict[str, int] | None = None,
    can: list[str] | None = None,
    *,
    console: list[str] | None = None,
    service: str = "",
    app: dict[str, object] | None = None,
) -> socket.socket | None:
    """A connection the agent accepted, or None; `can` and `console` list what the app offers the console."""
    sock = _open(addr)
    if sock is None:
        return None
    try:
        # The agent finds the issues, with this project's limits.
        hello = {
            "protocol": PROTOCOL,
            "max_traces": max_traces,
            **({"thresholds": thresholds} if thresholds else {}),
            **({"service": service} if service else {}),
            **({"capabilities": can} if can else {}),
            **({"console": console} if console else {}),
            **({"app": app} if app else {}),
        }
        sock.sendall(json.dumps(hello).encode() + b"\n")
        answer = json.loads(sock.makefile("rb").readline() or b"{}")
    except (OSError, ValueError):
        sock.close()
        return None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        sock.close()
        return None
    sock.settimeout(SEND_TIMEOUT_S)
    return sock


def _open(addr: str) -> socket.socket | None:
    """A connection to `unix:/path` or `tcp:host:port` (Windows; an agent outside the app's container)."""
    try:
        if addr.startswith("tcp:"):
            host, _, port = addr.removeprefix("tcp:").rpartition(":")
            sock = socket.create_connection((host.strip("[]"), int(port)), timeout=CONNECT_TIMEOUT_S)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock
        if addr.startswith("unix:") and hasattr(socket, "AF_UNIX"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT_S)
            try:
                sock.connect(addr.removeprefix("unix:"))
            except OSError:
                sock.close()
                raise
            return sock
    except (OSError, ValueError):
        return None
    return None


def _closed(sock: socket.socket) -> bool:
    """Whether the agent closed this connection: it sends nothing unasked, so anything to read is its end."""
    try:
        nothing: list[socket.socket] = []
        readable, _, _ = select.select([sock], nothing, nothing, 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class AgentWriter(BatchWriter):
    """Batches to the agent while it answers; without one, the batches are dropped (and counted)."""

    def __init__(self, config: conf.Config) -> None:
        self.config = config
        self.sent = 0
        self._sock: socket.socket | None = None
        self._retry_at = 0.0
        self._warned = False
        # The writer thread sends; a console asking what the app offers connects first (connected()). Free in a forked
        # child even if the parent's writer held it as the process forked (sending, starting the agent).
        self._lock = forks.lock()
        super().__init__()

    @override
    def _start(self) -> None:
        # After a fork the child makes its own connection, with its own hello: the parent's socket is the parent's.
        # The child's copy of it is closed (the parent's connection stays open), so no frame of the child's can go
        # into the parent's stream, between two of its frames.
        inherited, self._sock = getattr(self, "_sock", None), None
        if inherited is not None:
            with contextlib.suppress(OSError):
                inherited.close()
        self._retry_at = 0.0
        super()._start()

    def connected(self) -> bool:
        """Connect now if not yet (so the agent knows what this app offers the console); whether it is connected."""
        with self._lock:
            self._connect()
            return self._sock is not None

    def _connect(self) -> None:
        if self._sock is not None and _closed(self._sock):
            # The agent went (a restart, a rebuild): a write would still go into this socket and be lost, unseen.
            self._sock.close()
            self._sock = None
            self._retry_at = 0.0
        if self._sock is not None or time.monotonic() < self._retry_at:
            return
        thresholds = {
            "n_plus_one": self.config.n_plus_one_threshold,
            "n_plus_one_min_ms": self.config.n_plus_one_min_ms,
            "slow_sql_ms": self.config.slow_sql_ms,
            "slow_http_ms": self.config.slow_http_ms,
            "long_transaction_ms": self.config.long_transaction_ms,
        }
        can, console = capabilities(self.config), console_offers()
        app = appinfo.describe(self.config)

        def hello(addr: str) -> socket.socket | None:
            return connect(
                addr, self.config.max_traces, thresholds, can, console=console, service=self.config.service, app=app
            )

        addr = address(self.config)
        self._sock = hello(addr) if addr else None
        if self._sock is None and ensure_started(self.config):
            # No agent yet: one was started (once per process; a second one per store exits by itself).
            addr = address(self.config)
            self._sock = hello(addr) if addr else None
        if self._sock is None:
            self._retry_at = time.monotonic() + RETRY_S

    @override
    def write(self, batch: list[Trace]) -> None:
        with self._lock:
            self._send(batch)

    @override
    def write_events(self, events: list[Event]) -> None:
        # Not counted with the traces, sent or dropped: without an agent there is nothing to show them anyway.
        with self._lock:
            self._frame({"tasks": events})

    def tell(self, found: dict[str, object]) -> None:
        """What the app's checks found, told to the agent connected now; one connected later hears it in the hello."""
        with self._lock:
            if self._sock is not None:
                self._frame({"app": found})

    def _send(self, batch: list[Trace]) -> None:
        if self._frame([trace.to_dict() for trace in batch]):
            self.sent += len(batch)
            self._warned = False
            return
        self.dropped += len(batch)
        # "off" is the user's choice: nothing to say about it.
        if not self._warned and self.config.agent != "off":
            self._warned = True
            sys.stderr.write(f"tracepanel ▸ {unavailable(self.config)}\n")

    def _frame(self, payload: object) -> bool:
        """One frame to the agent; whether it went."""
        self._connect()
        if self._sock is None:
            return False
        data = json.dumps(payload, separators=(",", ":")).encode()
        try:
            self._sock.sendall(struct.pack(">I", len(data)) + data)
        except OSError:
            # The agent went away: this frame is lost; the next ones go to it once it is back.
            self._sock.close()
            self._sock = None
            self._retry_at = time.monotonic() + RETRY_S
            return False
        return True


def _checked(found: dict[str, object]) -> None:
    tell = getattr(transport.get_writer(), "tell", None)
    if callable(tell):
        tell(found)


appinfo.when_checked(_checked)
