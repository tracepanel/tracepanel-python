"""MongoDB through pymongo's command monitoring, on a server: TRACEPANEL_TEST_MONGO=mongodb://host:port."""

import asyncio
import os
from collections.abc import Iterator

import pytest
from dirty_equals import IsStr

pymongo = pytest.importorskip("pymongo")

from tests.agentstore import issues  # noqa: E402
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.model import Span, Trace  # noqa: E402
from tracepanel.integrations import mongodb as integration  # noqa: E402
from tracepanel.storage.transport import complete  # noqa: E402

URL = os.environ.get("TRACEPANEL_TEST_MONGO", "")
pytestmark = pytest.mark.skipif(not URL, reason="needs MongoDB")

integration.install()


@pytest.fixture
def users() -> Iterator[pymongo.collection.Collection]:
    client = pymongo.MongoClient(URL)
    collection = client["tracepanel_test"]["users"]
    collection.drop()
    collection.insert_many([{"_id": n, "name": f"user {n}"} for n in range(5)])
    yield collection
    collection.drop()
    client.close()


def traced(work: object) -> Trace:
    with start_trace("http", "GET /users") as trace:
        work()  # type: ignore[operator]
    return complete(trace)


def commands(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "mongodb"]


def test_a_command_is_a_span_with_its_shape_and_its_values_apart(users: pymongo.collection.Collection) -> None:
    trace = traced(lambda: users.find_one({"name": "user 3"}))

    [span] = commands(trace)
    assert (span.name, span.status) == ("find users", "ok")
    assert span.attrs == {
        "db.system": "mongodb",
        "db.operation": "find",
        "db.namespace": "tracepanel_test",
        "db.collection.name": "users",
        "db.statement": '{"find": "users", "filter": {"name": "?"}, "limit": 1, "singleBatch": true}',
        "db.params": ["user 3"],
        "db.params_hash": IsStr(regex=r"^[0-9a-f]{12}$"),
        "db.response.returned_rows": 1,
        "server.address": IsStr(),
        "server.port": int(URL.rsplit(":", 1)[1].split("/")[0]),
        "code.origin": IsStr(regex=r"tests/integrations/test_mongodb\.py:\d+ in <lambda>"),
    }


def test_a_find_per_item_is_an_n_plus_one_and_the_same_find_twice_a_duplicate(
    users: pymongo.collection.Collection,
) -> None:
    per_item = traced(lambda: [users.find_one({"_id": n}) for n in range(5)])
    twice = traced(lambda: [users.find_one({"_id": 1}) for _ in range(2)])

    assert [issue.kind for issue in issues(per_item)] == ["n+1"]
    assert [issue.kind for issue in issues(twice)] == ["duplicates"]


def test_lists_of_values_are_one_mark_and_values_stay_out_without_capture(
    users: pymongo.collection.Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPANEL_CAPTURE_SQL_PARAMS", "false")
    conf.reset()

    trace = traced(lambda: list(users.find({"_id": {"$in": [1, 2, 3]}})))

    [span] = commands(trace)
    assert span.attrs["db.statement"] == '{"find": "users", "filter": {"_id": {"$in": ["?"]}}}'
    assert "db.params" not in span.attrs
    assert span.attrs["db.response.returned_rows"] == 3


def test_a_failed_command_is_an_error_and_the_driver_s_own_are_left_out(
    users: pymongo.collection.Collection,
) -> None:
    def work() -> None:
        users.database.client.admin.command("ping")
        with pytest.raises(pymongo.errors.OperationFailure):
            list(users.aggregate([{"$nope": {}}]))

    [span] = commands(traced(work))

    assert (span.name, span.status, span.attrs["error.type"]) == ("aggregate users", "error", IsStr())


def test_the_async_client() -> None:
    async def work() -> None:
        client = pymongo.AsyncMongoClient(URL)
        try:
            await client["tracepanel_test"]["things"].insert_one({"n": 1})
            await client["tracepanel_test"]["things"].drop()
        finally:
            await client.close()

    trace = traced(lambda: asyncio.run(work()))

    assert [span.name for span in commands(trace)] == ["insert things", "drop things"]
    assert (
        commands(trace)[0].attrs["db.statement"]
        == '{"insert": "things", "ordered": true, "documents": [{"n": "?", "_id": "?"}]}'
    )
