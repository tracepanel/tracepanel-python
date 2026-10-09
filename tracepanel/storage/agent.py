"""Sending traces to tracepanel-agent, which writes the project's local store in place of each process.

The agent (`tracepanel agent`) leaves its address in `<store dir>/agent.addr`. While it answers, a process only
serializes its traces and sends them in batches; the SQLite work, and its share of the CPU, is the agent's. Without
one (or one for another store version), the process writes the store itself, as it does without an agent, and looks
for one again a few seconds later: starting or stopping the agent never loses traces.

The wire: a hello line, `{"protocol": 1, "store": "13", "max_traces": 2000}`, answered by `{"ok": true}` (or a refusal
with its reason); then frames, a 4-byte big-endian length and a JSON array of `Trace.to_dict()`.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from tracepanel._agent import AGENT, find_agent_bin
from tracepanel._compat import override
from tracepanel.core import conf
from tracepanel.core.model import Trace
from tracepanel.storage import transport
from tracepanel.storage.store import STORE_VERSION, Store, get_store
from tracepanel.storage.transport import BatchWriter

PROTOCOL = 1
# Issues are found by the agent, off the app's processes; True: this SDK finds them itself (the tests that compare
# what the agent writes with what Python writes).
ANALYZED = False
ADDR_FILE = "agent.addr"
# Where the agent serves the console (with --serve): an app's /__tracepanel__/ sends its API calls there.
CONSOLE_FILE = "console.addr"
LOG_FILE = "agent.log"
# An agent an SDK started exits after this long unused; how long the SDK waits for it to answer.
IDLE_EXIT_S = 1800
START_WAIT_S = 1.0
# How long to wait for the agent to answer; the batch then goes to the local store.
CONNECT_TIMEOUT_S = 0.5
# A stuck agent must not hold the writer thread (and the queue behind it) for long.
SEND_TIMEOUT_S = 2.0
# Without an agent, how often to look for one again.
RETRY_S = 5.0


def store_dir(config: conf.Config) -> Path:
    return conf.project_root() / config.dir


def binary() -> str | None:
    """The tracepanel-agent executable: $TRACEPANEL_AGENT_BIN, the installed one, or a sibling checkout's build."""
    if explicit := os.environ.get("TRACEPANEL_AGENT_BIN"):
        return explicit if Path(explicit).is_file() else None
    if found := find_agent_bin():
        return found
    # tracepanel-python used from its checkout (development): a tracepanel-agent checkout next to it, its newest build.
    target = Path(__file__).resolve().parents[2].parent / "tracepanel-agent" / "target"
    builds = [build for build in (target / "release" / AGENT, target / "debug" / AGENT) if build.is_file()]
    return str(max(builds, key=lambda build: build.stat().st_mtime)) if builds else None


def can_start(config: conf.Config) -> bool:
    """Start an agent on demand: with the "auto" setting, and never under a test runner."""
    return config.agent == "auto" and "pytest" not in sys.modules


def start(config: conf.Config) -> subprocess.Popen[bytes] | None:
    """Start the project's agent, serving the console too; its own session, so an app's restarts don't stop it.

    It exits after IDLE_EXIT_S unused. Returns the process once it answers (writes its address), else None.
    """
    executable = binary()
    if executable is None:
        return None
    directory = store_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    command = [executable, "--dir", str(directory), "--serve", "127.0.0.1:0", "--idle-exit", str(IDLE_EXIT_S)]
    with (directory / LOG_FILE).open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 (tracepanel's own agent)
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if (directory / ADDR_FILE).exists():
            sys.stderr.write(f"tracepanel ▸ agent started for {directory}\n")
            return process
        if process.poll() is not None:
            # Another process's agent got there first (one agent per store): its address is there.
            if not (directory / ADDR_FILE).exists():
                _not_started(directory)
            return None
        time.sleep(0.02)
    _not_started(directory)
    return None


def _not_started(directory: Path) -> None:
    sys.stderr.write(
        f"tracepanel ▸ the agent didn't start (see {directory / LOG_FILE}): traces go to the store directly\n"
    )


_start_lock = threading.Lock()
_start_tried = False


def ensure_started(config: conf.Config) -> bool:
    """An agent for this store: one answering already, or one started now (once per process). True if there is one."""
    global _start_tried  # noqa: PLW0603 (once per process)
    if address(config) and console_url(config):
        return True
    if not can_start(config):
        return False
    with _start_lock:
        if not _start_tried:
            _start_tried = True
            # Another process's agent may win the race (one per store): then its address is there.
            start(config)
    return address(config) is not None


def console_url(config: conf.Config) -> str | None:
    """`http://127.0.0.1:PORT/` of the agent's console, when an agent serves this store's."""
    if config.agent == "off":
        return None
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


def connect(addr: str, max_traces: int, thresholds: dict[str, int] | None = None) -> socket.socket | None:
    """A connection the agent accepted for this store version, or None."""
    if not addr.startswith("unix:"):
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.connect(addr.removeprefix("unix:"))
        # Traces come without their issues: the agent finds them, as for SDKs that never analyze (Go).
        hello = {
            "protocol": PROTOCOL,
            "store": STORE_VERSION,
            "max_traces": max_traces,
            "analyzed": ANALYZED,
            # The limits issues are found with, should the agent analyze.
            **({"thresholds": thresholds} if thresholds else {}),
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


class AgentWriter(BatchWriter):
    """Batches to the agent while it answers; to the local store otherwise."""

    def __init__(self, config: conf.Config) -> None:
        self.config = config
        self.analyzes_later = not ANALYZED
        self.sent = 0
        self._sock: socket.socket | None = None
        self._retry_at = 0.0
        self._store: Store | None = None
        super().__init__()

    @override
    def _start(self) -> None:
        # After a fork the child has its own connection to make: the parent's socket is the parent's.
        self._sock = None
        self._retry_at = 0.0
        super()._start()

    @override
    def write(self, batch: list[Trace]) -> None:
        if self._sock is None and time.monotonic() >= self._retry_at:
            addr = address(self.config)
            thresholds = {
                "n_plus_one": self.config.n_plus_one_threshold,
                "slow_sql_ms": self.config.slow_sql_ms,
                "long_transaction_ms": self.config.long_transaction_ms,
            }
            self._sock = connect(addr, self.config.max_traces, thresholds) if addr else None
            if self._sock is None and ensure_started(self.config):
                # No agent yet: one was started (once per process; a second one per store exits by itself).
                addr = address(self.config)
                self._sock = connect(addr, self.config.max_traces, thresholds) if addr else None
            if self._sock is None:
                self._retry_at = time.monotonic() + RETRY_S
        if self._sock is not None:
            data = json.dumps([trace.to_dict() for trace in batch], separators=(",", ":")).encode()
            try:
                self._sock.sendall(struct.pack(">I", len(data)) + data)
            except OSError:
                # The agent went away: this batch, and the next ones until it is back, go to the local store.
                self._sock.close()
                self._sock = None
                self._retry_at = time.monotonic() + RETRY_S
            else:
                self.sent += len(batch)
                return
        if self._store is None:
            self._store = get_store()
        if self.analyzes_later:
            # Without the agent the issues are found here, in this writer's thread, still off the request.
            for trace in batch:
                trace.issues = transport.analysis(trace, self.config)
        self._store.write(batch)
