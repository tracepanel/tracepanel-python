"""Projects without Django's optional apps (auth, contenttypes, sessions): tracepanel starts and traces requests.

INSTALLED_APPS is fixed per process: each project runs in a fresh interpreter, which prints what it recorded.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dirty_equals import IsInt, IsStr

ROOT = Path(__file__).resolve().parent.parent.parent

TRACE_A_REQUEST = """
import json
import django
django.setup()
from django.test import Client
from tracepanel.storage import agent, transport

traces = []


class Writer:
    def submit(self, trace):
        traces.append(trace)

    def flush(self, timeout=5.0):
        pass


transport.set_writer(Writer())
status = Client().get("/ping/").status_code
print(json.dumps({"status": status, "console": agent.console_offers(), "traces": [t.to_dict() for t in traces]}))
"""


def run(settings: str) -> dict[str, object]:
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("TRACEPANEL_")},
        "TRACEPANEL_AGENT": "off",
        "DJANGO_SETTINGS_MODULE": settings,
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(  # noqa: S603 (our own interpreter)
        [sys.executable, "-c", TRACE_A_REQUEST], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize(
    "settings",
    [
        # No auth, contenttypes or sessions at all.
        "tests.project.settings_bare",
        # Auth without sessions: switching the user needs a session to log into.
        "tests.project.settings_no_sessions",
    ],
)
def test_a_request_is_traced_and_user_switching_is_not_offered(settings: str) -> None:
    recorded = run(settings)

    # The console offers EXPLAIN only: no "users", so the widget has no button to switch the user.
    assert recorded == {
        "status": 200,
        "console": ["explain"],
        "traces": [TRACE],
    }

    spans = recorded["traces"][0]["spans"]  # type: ignore[index]
    assert [span["parent_id"] for span in spans] == [None, spans[0]["id"], spans[1]["id"]]


ULID = IsStr(regex=r"^[0-9A-Z]{26}$")
TRACE: dict[str, object] = {
    "id": ULID,
    "kind": "http",
    "name": "GET /ping/",
    "start": IsInt,
    "duration": IsInt,
    "status": "ok",
    "attrs": {
        "http.method": "GET",
        "http.path": "/ping/",
        "http.route": "/ping/",
        "code.view": "tests.project.urls_bare.ping",
        "http.status_code": 200,
        "http.response.header.content_type": "text/html; charset=utf-8",
        "http.response.body.size": 4,
        "service.name": IsStr,  # the name of the directory the tests run in
        "service.role": "web",
        "telemetry.sdk.language": "python",
        "process.runtime.name": IsStr,
        "process.runtime.version": IsStr,
        "tracepanel.framework": IsStr(regex=r"Django \d.*"),
    },
    "spans": [
        {
            "id": ULID,
            "parent_id": None,
            "kind": "http",
            "name": "GET /ping/",
            "start": IsInt,
            "duration": IsInt,
            "status": "ok",
            "attrs": {},
        },
        {
            "id": ULID,
            "parent_id": ULID,
            "kind": "view",
            "name": "tests.project.urls_bare.ping",
            "start": IsInt,
            "duration": IsInt,
            "status": "ok",
            "attrs": {"code.view": "tests.project.urls_bare.ping"},
        },
        {
            "id": ULID,
            "parent_id": ULID,
            "kind": "sql",
            "name": "SELECT",
            "start": IsInt,
            "duration": IsInt,
            "status": "ok",
            "attrs": {
                "db.client": "django",
                "db.alias": "default",
                "db.vendor": "sqlite",
                "db.system": "sqlite",
                "db.namespace": None,
                "db.statement": "SELECT 1",
                "db.fingerprint": "SELECT 1",
                "db.params_hash": IsStr,
                "db.many": False,
                "code.origin": "tests/project/urls_bare.py:10 in ping",
                "code.template": None,
            },
        },
    ],
    "logs": [],
    "errors": [],
    "links": [],
    "issues": [],
}
