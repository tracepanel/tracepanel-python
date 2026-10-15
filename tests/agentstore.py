"""The project's store, as the tests see it: a real tracepanel-agent, read through the console's API.

Python only records and sends; the agent keeps the traces, finds their issues and answers the console. AgentStore
has the reads the tests need, each one an API call, returning what the API returns (lists unwrapped).
"""

import atexit
import dataclasses
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from tracepanel._compat import override
from tracepanel.core import conf
from tracepanel.core.model import Issue, Trace
from tracepanel.storage import agent


def newer(expected: dict[str, Any], answer: Mapping[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """The expected answer, with the fields an agent newer than the released one adds, where this agent has them.

    CI runs the released agent, a local run the sibling one, ahead of it: both are held to their whole answer.
    """
    return {**expected, **{key: value for key, value in fields.items() if key in answer}}


# The agent's schemas (store, search, the console's API): its checkout next to this one, or $TRACEPANEL_AGENT_DIR.
AGENT_SCHEMAS = (
    Path(os.environ.get("TRACEPANEL_AGENT_DIR") or Path(__file__).resolve().parents[2] / "tracepanel") / "schema"
)
WAIT_S = 10.0
# What the API answers, as JSON.
Json = dict[str, Any]
POLL_S = 0.02


def binary() -> str:
    """The agent under test: $TRACEPANEL_AGENT_BIN, else the one uv built into this environment."""
    found = os.environ.get("TRACEPANEL_AGENT_BIN") or agent.binary()
    if not found or not Path(found).is_file():
        msg = "no tracepanel-agent: `uv sync` builds it from ../tracepanel, or set TRACEPANEL_AGENT_BIN"
        raise RuntimeError(msg)
    return found


class SearchError(Exception):
    """The API refused a search (400): the message the console shows."""


@dataclass
class AgentStore:
    directory: Path
    process: subprocess.Popen[bytes]
    address: str
    console: str
    # The store's size the next write sends (the agent prunes to it), as the `max_traces` setting does.
    max_traces: int = 5
    _sent: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, directory: Path, *args: str) -> "AgentStore":
        command = [binary(), "--dir", str(directory), "--serve", "127.0.0.1:0", *args]
        addr, console = directory / agent.ADDR_FILE, directory / agent.CONSOLE_FILE
        # Another agent's address (one per store): this one hasn't started until it writes its own.
        before = addr.read_text() if addr.exists() else None
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # noqa: S603
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            written = addr.read_text() if addr.exists() else ""
            if written and written != before and console.exists() and console.read_text():
                return cls(directory, process, written.strip(), console.read_text().strip())
            time.sleep(POLL_S)
        process.kill()
        msg = f"the agent did not start: {process.communicate()[0].decode(errors='replace')}"
        raise RuntimeError(msg)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=WAIT_S)

    def config(self) -> conf.Config:
        """Settings whose writer sends to this agent."""
        return dataclasses.replace(conf.get_config(), agent=self.address, max_traces=self.max_traces)

    # ---------- writing ----------

    def write(self, traces: list[Trace]) -> None:
        """Send the traces as an SDK does, and wait until the agent has stored them."""
        if not traces:
            return
        config = conf.get_config()
        thresholds = {
            "n_plus_one": config.n_plus_one_threshold,
            "n_plus_one_min_ms": config.n_plus_one_min_ms,
            "slow_sql_ms": config.slow_sql_ms,
            "slow_http_ms": config.slow_http_ms,
            "long_transaction_ms": config.long_transaction_ms,
        }
        sock = agent.connect(self.address, self.max_traces, thresholds)
        if sock is None:
            msg = "the agent refused the connection"
            raise RuntimeError(msg)
        with sock:
            data = json.dumps([trace.to_dict() for trace in traces], separators=(",", ":")).encode()
            sock.sendall(struct.pack(">I", len(data)) + data)
        self.wait_for(traces[-1].id)

    def wait_for(self, trace_id: str) -> None:
        """Until the agent has written this trace (it writes in its own thread)."""
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            if self.list_traces(ids=[trace_id]):
                return
            time.sleep(POLL_S)
        msg = f"the agent did not store {trace_id}"
        raise TimeoutError(msg)

    def writer(self) -> "SyncedWriter":
        """The SDK's own writer, sending to this agent."""
        return SyncedWriter(self)

    def clear(self) -> int:
        deleted = self.call("store", verb="DELETE")
        return int(deleted["deleted"])

    # ---------- reading: the console's API ----------

    def call(self, path: str, query: dict[str, object] | None = None, *, verb: str = "GET") -> Any:  # noqa: ANN401 (JSON)
        params = {key: str(value) for key, value in (query or {}).items() if value is not None}
        url = f"{self.console}api/{path}" + (f"?{urlencode(params)}" if params else "")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=verb), timeout=WAIT_S) as response:  # noqa: S310
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = json.loads(error.read() or b"{}")
            if error.code == 400:
                raise SearchError(body.get("error", "")) from None
            if error.code == 404:
                return None
            raise

    def get(self, path: str, **query: object) -> Json:
        """An answer that is an object."""
        answer = self.call(path, query)
        if not isinstance(answer, dict):
            msg = f"api/{path}: {answer!r}"
            raise TypeError(msg)
        return answer

    def list_traces(self, *, limit: int = 100, ids: list[str] | None = None) -> list[Json]:
        return self.get("traces", limit=limit, ids=",".join(ids) if ids else None)["traces"]

    def search_traces(
        self, query: str = "", *, limit: int = 100, before: int | None = None
    ) -> tuple[list[Json], int | None]:
        page = self.get("traces", q=query, limit=limit, before=before)
        return page["traces"], page["next"]

    def get_trace(self, trace_id: str) -> Json | None:
        return self.call(f"traces/{trace_id}")

    def related(self, trace_id: str) -> Json:
        return self.get(f"traces/{trace_id}/related")

    def search_logs(
        self, query: str = "", *, limit: int = 100, before: int | None = None
    ) -> tuple[list[Json], int | None]:
        page = self.get("logs", q=query, limit=limit, before=before)
        return page["logs"], page["next"]

    def list_mails(
        self, query: str = "", *, limit: int = 100, before: int | None = None
    ) -> tuple[list[Json], int | None]:
        page = self.get("mails", q=query, limit=limit, before=before)
        return page["mails"], page["next"]

    def _list(self, path: str, query: str, limit: int) -> list[Json]:
        return self.get(path, q=query, limit=limit)[path.split("/", maxsplit=1)[0]]

    def list_calls(self, system: str = "", query: str = "", *, limit: int = 100) -> list[Json]:
        return self.get("calls", system=system, q=query, limit=limit)["calls"]

    def list_endpoints(self, query: str = "", *, limit: int = 100) -> list[Json]:
        return self._list("endpoints", query, limit)

    def list_tasks(self, query: str = "", *, limit: int = 100) -> list[Json]:
        return self._list("tasks", query, limit)

    def list_jobs(self, query: str = "", *, limit: int = 100) -> list[Json]:
        return self._list("jobs", query, limit)

    def list_websockets(self, query: str = "", *, limit: int = 100) -> list[Json]:
        return self._list("websockets", query, limit)

    def list_graphql(self, query: str = "", *, limit: int = 100) -> list[Json]:
        return self._list("graphql", query, limit)

    def list_issues(self, query: str = "", *, limit: int = 100) -> list[Json]:
        return self._list("issues", query, limit)

    def list_services(self, query: str = "") -> Json:
        return self.get("services", q=query)

    def graphql_detail(self, operation: str) -> Json | None:
        return self.call("graphql/detail", {"operation": operation})

    def summary(self, query: str) -> Json | None:
        return self.call("traces/summary", {"q": query})

    def endpoint_detail(self, method: str, route: str) -> Json | None:
        return self.call("endpoints/detail", {"method": method, "route": route})

    def issue_detail(self, fingerprint: str) -> Json | None:
        return self.call("issues/detail", {"fingerprint": fingerprint})

    def traces_overview(self, query: str = "", *, group: str = "operation") -> Json:
        return self.get("traces/overview", q=query, group=group)

    def known_values(self) -> Json:
        return self.get("values")

    def counts(self) -> Json:
        return self.get("counts")

    def issues_of(self, trace: Trace) -> list[Json]:
        """The issues the agent found in one trace (written first)."""
        self.write([trace])
        document = self.get_trace(trace.id)
        issues = document.get("issues") if document else None
        return issues if isinstance(issues, list) else []


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class SyncedWriter(agent.AgentWriter):
    """The SDK's writer to a test's agent, whose flush() also waits until the agent has stored what was sent."""

    def __init__(self, store: AgentStore) -> None:
        self.store = store
        self.last: str | None = None
        super().__init__(store.config())

    @override
    def write(self, batch: list[Trace]) -> None:
        super().write(batch)
        self.last = batch[-1].id

    @override
    def flush(self, timeout: float = 5.0) -> None:
        super().flush(timeout)
        # At exit (BatchWriter flushes then too) the test's agent is gone: nothing to wait for.
        if self.last is not None and self.store.process.poll() is None:
            self.store.wait_for(self.last)
            self.last = None


_shared: AgentStore | None = None


def issues(trace: Trace) -> list[Issue]:
    """The issues the agent finds in a trace: a session's own agent, started on first use, stopped at exit."""
    global _shared  # noqa: PLW0603 (one agent for the whole run)
    if _shared is None:
        _shared = AgentStore.start(Path(tempfile.mkdtemp(prefix="tracepanel-tests-")) / ".tracepanel")
        _shared.max_traces = 100_000
        atexit.register(_shared.stop)
    names = {item.name for item in dataclasses.fields(Issue)}
    found: list[Json] = _shared.issues_of(trace)
    return [Issue(**{name: item[name] for name in names if name in item}) for item in found]
