"""A stand-in for Snowflake's REST API, over plain HTTP on a local port, so no account is needed.

Enough for the connector to log in, run a query and close its session. The connector connects to it with `host`,
`port` and `protocol="http"`. Each statement answers one row as JSON: the number 1, or what a session function
(CURRENT_VERSION()) says.
"""

import gzip
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from tracepanel._compat import override

DATABASE = "ANALYTICS"
SCHEMA = "PUBLIC"

_SESSION: dict[str, Any] = {
    "token": "session-token",
    "masterToken": "master-token",
    "validityInSeconds": 3600,
    "masterValidityInSeconds": 14400,
    "sessionId": 1,
    "parameters": [],
    "sessionInfo": {
        "databaseName": DATABASE,
        "schemaName": SCHEMA,
        "warehouseName": "COMPUTE_WH",
        "roleName": "ANALYST",
    },
}

_ROW_TYPE = {
    "name": "1",
    "database": "",
    "schema": "",
    "table": "",
    "type": "fixed",
    "nullable": False,
    "scale": 0,
    "precision": 1,
    "length": None,
    "byteLength": None,
    "collation": None,
}


# What a session function answers: SQLAlchemy's dialect asks them when it connects first.
_FUNCTIONS = {
    "CURRENT_VERSION()": "9.30.0",
    "CURRENT_DATABASE()": DATABASE,
    "CURRENT_SCHEMA()": SCHEMA,
}


def _answer_to(statement: str) -> tuple[list[dict[str, Any]], list[str]]:
    """A statement's columns and its row: the session functions it asks, in their order, else the number 1."""
    asked = statement.upper()
    found = sorted((asked.index(function), function) for function in _FUNCTIONS if function in asked)
    if not found:
        return [_ROW_TYPE], ["1"]
    columns = [{**_ROW_TYPE, "name": name, "type": "text", "scale": None, "precision": None} for _, name in found]
    return columns, [_FUNCTIONS[name] for _, name in found]


def _result(query_id: str, statement: str) -> dict[str, Any]:
    columns, row = _answer_to(statement)
    return {
        "parameters": [],
        "rowtype": columns,
        "rowset": [row],
        "total": 1,
        "returned": 1,
        "queryId": query_id,
        "queryResultFormat": "json",
        "statementTypeId": 4096,
        "finalDatabaseName": DATABASE,
        "finalSchemaName": SCHEMA,
        "finalWarehouseName": "COMPUTE_WH",
        "finalRoleName": "ANALYST",
    }


class Handler(BaseHTTPRequestHandler):
    # The paths asked for, in order, without their query strings.
    paths: ClassVar[list[str]] = []
    _queries = 0

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        Handler.paths.append(path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        if path == "/session/v1/login-request":
            data: object = _SESSION
        elif path == "/queries/v1/query-request":
            Handler._queries += 1
            data = _result(f"01b0-{Handler._queries:04d}", str(json.loads(body or b"{}").get("sqlText", "")))
        else:
            # The session's end, telemetry, a heartbeat.
            data = dict[str, object]()
        self._answer({"success": True, "code": None, "message": None, "data": data})

    def do_GET(self) -> None:
        self.do_POST()

    def _answer(self, payload: object) -> None:
        answer = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    @override
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 (http.server's signature)
        pass


@contextmanager
def serving() -> Iterator[int]:
    """The stand-in on a free local port, for the block: its port."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def params(port: int) -> dict[str, Any]:
    """What connect() takes to reach the stand-in."""
    return {
        "account": "acme",
        "user": "analyst",
        "password": "secret",
        "host": "127.0.0.1",
        "port": port,
        "protocol": "http",
        "database": DATABASE,
        "schema": SCHEMA,
        "warehouse": "COMPUTE_WH",
        # No browser, no keep-alive thread, no usage reports: the requests a test makes are its own.
        "client_session_keep_alive": False,
        "login_timeout": 10,
    }
