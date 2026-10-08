import json

from dirty_equals import IsStr

from tracepanel.console import server
from tracepanel.core.ids import new_id
from tracepanel.core.model import Span, Trace
from tracepanel.storage.store import Store


def get(path: str, query: str = "") -> tuple[str, dict[str, str], bytes]:
    seen: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen.update(status=status, headers=dict(headers))

    body = b"".join(server.wsgi({"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query}, start_response))
    return str(seen["status"]), dict(seen["headers"]), body  # type: ignore[arg-type]


def test_api_reads_the_store(store: Store) -> None:
    trace = Trace(id=new_id(), kind="task", name="blog.tasks.digest", start=0, duration=1000)
    trace.spans.append(Span(id=new_id(), parent_id=None, kind="task.run", name=trace.name, start=0, duration=1000))
    store.write([trace])

    status, headers, body = get(f"/api/traces/{trace.id}")

    assert (status, headers["Content-Type"]) == ("200 OK", "application/json")
    assert json.loads(body)["name"] == "blog.tasks.digest"


def test_unknown_trace(store: Store) -> None:
    status, _, body = get(f"/api/traces/{new_id()}")

    assert (status, json.loads(body)) == ("404 Not Found", {"error": "not found"})


def test_page_is_served() -> None:
    status, headers, _ = get("/")

    assert status == IsStr(regex=r"(200 OK|404 Not Found)")
    assert headers["Content-Type"] in {"text/html", "application/json"}
