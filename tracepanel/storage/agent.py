"""Sending traces to tracepanel-agent, which writes the project's local store in place of each process.

The agent (`tracepanel agent`) leaves its address in `<store dir>/agent.addr`. While it answers, a process only
serializes its traces and sends them in batches; the SQLite work, and its share of the CPU, is the agent's. Without
one (or one for another store version), the process writes the store itself, as it does without an agent, and looks
for one again a few seconds later: starting or stopping the agent never loses traces.

The wire: a hello line, `{"protocol": 1, "store": "13", "max_traces": 2000}`, answered by `{"ok": true}` (or a refusal
with its reason); then frames, a 4-byte big-endian length and a JSON array of `Trace.to_dict()`.
"""

import json
import socket
import struct
import time

from tracepanel._compat import override
from tracepanel.core import conf
from tracepanel.core.model import Trace
from tracepanel.storage.store import STORE_VERSION, Store, get_store
from tracepanel.storage.transport import BatchWriter

PROTOCOL = 1
ADDR_FILE = "agent.addr"
# How long to wait for the agent to answer; the batch then goes to the local store.
CONNECT_TIMEOUT_S = 0.5
# A stuck agent must not hold the writer thread (and the queue behind it) for long.
SEND_TIMEOUT_S = 2.0
# Without an agent, how often to look for one again.
RETRY_S = 5.0


def address(config: conf.Config) -> str | None:
    """`unix:/path` of the agent: the `agent` setting, or with "auto" the file the agent writes."""
    if config.agent != "auto":
        return config.agent or None
    try:
        return (conf.project_root() / config.dir / ADDR_FILE).read_text().strip() or None
    except OSError:
        return None


def connect(addr: str, max_traces: int) -> socket.socket | None:
    """A connection the agent accepted for this store version, or None."""
    if not addr.startswith("unix:"):
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.connect(addr.removeprefix("unix:"))
        hello = {"protocol": PROTOCOL, "store": STORE_VERSION, "max_traces": max_traces}
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
            self._sock = connect(addr, self.config.max_traces) if addr else None
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
        self._store.write(batch)
