"""AWS calls through botocore, against moto's AWS served from a thread of this process: no credentials, no AWS."""

import asyncio
import io
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from dirty_equals import IsPartialDict, IsStr
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")

from aiobotocore.session import get_session  # noqa: E402 (optional test group)
from boto3.dynamodb.conditions import Key  # noqa: E402
from botocore.awsrequest import AWSResponse  # noqa: E402
from botocore.compat import HTTPHeaders  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402
from django.core.files.storage import storages  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402

from tests.agentstore import issues  # noqa: E402
from tracepanel.core.context import start_span, start_trace  # noqa: E402
from tracepanel.core.model import Span, Trace  # noqa: E402
from tracepanel.integrations import boto as integration  # noqa: E402
from tracepanel.integrations import http_clients  # noqa: E402
from tracepanel.storage.transport import complete  # noqa: E402

http_clients.install()
integration.install()

REGION = "eu-west-1"
REQUEST_ID = IsStr(min_length=1)
CREDENTIALS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}


def origin(test: str) -> IsStr:
    return IsStr(regex=rf"tests/integrations/test_boto\.py:\d+ in {test}")


@pytest.fixture(scope="module")
def endpoint() -> Iterator[str]:
    """Moto's AWS in a thread: the clients reach it over HTTP, as they would AWS. No credentials of this machine."""
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        env.setenv("AWS_CONFIG_FILE", "/dev/null")
        server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        server.start()
        host, port = server.get_host_and_port()
        yield f"http://{host}:{port}"
        server.stop()


@pytest.fixture
def client(endpoint: str) -> Callable[[str], Any]:
    def make(service: str) -> Any:  # noqa: ANN401 (a botocore client)
        return boto3.client(service, endpoint_url=endpoint, region_name=REGION, **CREDENTIALS)

    return make


def traced(work: Callable[[], object]) -> Trace:
    with start_trace("http", "GET /") as trace:
        work()
    return complete(trace)


def calls(trace: Trace) -> list[Span]:
    return trace.spans[1:]


def where(endpoint: str) -> dict[str, object]:
    port = int(endpoint.rpartition(":")[2])
    return {"server.address": "127.0.0.1", "server.port": port}


# ---------- S3 ----------


def test_s3_calls_are_storage_spans(
    client: Callable[[str], Any], endpoint: str, validator: Draft202012Validator
) -> None:
    s3 = client("s3")
    s3.create_bucket(Bucket="avatars", CreateBucketConfiguration={"LocationConstraint": REGION})

    def work() -> None:
        s3.put_object(Bucket="avatars", Key="users/1.png", Body=b"png!")
        s3.get_object(Bucket="avatars", Key="users/1.png")["Body"].read()
        s3.list_objects_v2(Bucket="avatars", Prefix="users/")

    trace = traced(work)
    validator.validate(trace.to_dict())
    s3_call = {
        "rpc.system": "aws-api",
        "rpc.service": "S3",
        "cloud.region": REGION,
        "storage.backend": "botocore.client.S3",
        "storage.system": "s3",
        "aws.s3.bucket": "avatars",
        **where(endpoint),
        "code.origin": origin("work"),
        "aws.request_id": REQUEST_ID,
        "http.response.status_code": 200,
    }

    # Their HTTP requests are part of them: no http.client spans.
    assert [(span.kind, span.name, span.status) for span in calls(trace)] == [
        ("storage", "S3 PutObject avatars/users/1.png", "ok"),
        ("storage", "S3 GetObject avatars/users/1.png", "ok"),
        ("storage", "S3 ListObjectsV2 avatars/users/", "ok"),
    ]

    assert [span.attrs for span in calls(trace)] == [
        {
            **s3_call,
            "rpc.method": "PutObject",
            "storage.op": "PutObject",
            "storage.path": "avatars/users/1.png",
            "aws.s3.key": "users/1.png",
            "storage.bytes": 4,
        },
        {
            **s3_call,
            "rpc.method": "GetObject",
            "storage.op": "GetObject",
            "storage.path": "avatars/users/1.png",
            "aws.s3.key": "users/1.png",
            "storage.bytes": 4,
        },
        {**s3_call, "rpc.method": "ListObjectsV2", "storage.op": "ListObjectsV2", "storage.path": "avatars/users/"},
    ]


def test_a_failed_call_has_aws_s_error_code(client: Callable[[str], Any]) -> None:
    s3 = client("s3")
    s3.create_bucket(Bucket="invoices", CreateBucketConfiguration={"LocationConstraint": REGION})

    def work() -> None:
        with pytest.raises(ClientError):
            s3.get_object(Bucket="invoices", Key="missing.pdf")

    (span,) = calls(traced(work))

    assert (span.name, span.status) == ("S3 GetObject invoices/missing.pdf", "error")

    assert span.attrs["error.type"] == "NoSuchKey"
    assert span.attrs["http.response.status_code"] == 404
    assert span.attrs["aws.request_id"] == REQUEST_ID


def test_retries_are_counted_on_the_call(client: Callable[[str], Any]) -> None:
    s3 = client("s3")
    s3.create_bucket(Bucket="reports", CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_object(Bucket="reports", Key="q3.csv", Body=b"a,b")
    failures = [503]

    class Raw:
        def stream(self, **kwargs: object) -> Iterator[bytes]:
            del kwargs
            yield b""

    def unavailable(request: Any, **kwargs: object) -> AWSResponse | None:  # noqa: ANN401 (botocore's request)
        del kwargs
        # The first attempt fails as AWS does when it is busy; botocore tries again.
        return AWSResponse(request.url, failures.pop(), HTTPHeaders(), Raw()) if failures else None

    s3.meta.events.register("before-send.s3.HeadObject", unavailable)

    (span,) = calls(traced(lambda: s3.head_object(Bucket="reports", Key="q3.csv")))

    assert (span.name, span.status) == ("S3 HeadObject reports/q3.csv", "ok")

    assert span.attrs["aws.retry_attempts"] == 1
    assert span.attrs["storage.bytes"] == 3


def test_a_transfer_s_requests_from_its_threads_are_in_the_trace(client: Callable[[str], Any]) -> None:
    s3 = client("s3")
    s3.create_bucket(Bucket="backups", CreateBucketConfiguration={"LocationConstraint": REGION})

    def work() -> None:
        s3.upload_fileobj(io.BytesIO(b"dump"), "backups", "db.sql")
        s3.download_fileobj("backups", "db.sql", io.BytesIO())

    trace = traced(work)

    assert [(span.kind, span.name, span.parent_id) for span in calls(trace)] == [
        ("storage", "S3 PutObject backups/db.sql", trace.spans[0].id),
        ("storage", "S3 HeadObject backups/db.sql", trace.spans[0].id),
        ("storage", "S3 GetObject backups/db.sql", trace.spans[0].id),
    ]


def test_async_clients(endpoint: str) -> None:
    async def work() -> None:
        session = get_session()
        async with session.create_client("s3", endpoint_url=endpoint, region_name=REGION, **CREDENTIALS) as s3:
            await s3.create_bucket(Bucket="exports", CreateBucketConfiguration={"LocationConstraint": REGION})
            await s3.put_object(Bucket="exports", Key="all.json", Body=b"[]")

    trace = traced(lambda: asyncio.run(work()))

    assert [(span.kind, span.name) for span in calls(trace)] == [
        ("storage", "S3 CreateBucket exports"),
        ("storage", "S3 PutObject exports/all.json"),
    ]

    assert calls(trace)[1].attrs["storage.backend"] == "aiobotocore.client.S3"
    assert calls(trace)[1].attrs["code.origin"] == origin("work")


# ---------- a storage's calls ----------


def test_a_call_inside_a_storage_call_keeps_its_kind(client: Callable[[str], Any]) -> None:
    s3 = client("s3")
    s3.create_bucket(Bucket="media", CreateBucketConfiguration={"LocationConstraint": REGION})

    def work() -> None:
        with start_span("storage", "storage exists a.png", {"storage.op": "exists"}), pytest.raises(ClientError):
            s3.head_object(Bucket="media", Key="a.png")

    storage, call = calls(traced(work))

    # Its own kind and attributes: the agent counts it as the storage call's.
    assert (call.kind, call.name, call.parent_id) == ("storage", "S3 HeadObject media/a.png", storage.id)

    assert call.attrs == IsPartialDict(
        {
            "rpc.system": "aws-api",
            "rpc.service": "S3",
            "rpc.method": "HeadObject",
            "cloud.region": REGION,
            "storage.system": "s3",
            "storage.op": "HeadObject",
            "storage.path": "media/a.png",
            "aws.s3.key": "a.png",
            "error.type": "404",
        }
    )


def test_django_storages_s3_calls_nest_under_its_storage_spans(endpoint: str, settings: Settings) -> None:
    pytest.importorskip("storages")
    boto3.client("s3", endpoint_url=endpoint, region_name=REGION, **CREDENTIALS).create_bucket(
        Bucket="uploads", CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        "uploads": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": "uploads",
                "endpoint_url": endpoint,
                "region_name": REGION,
                "access_key": "testing",
                "secret_key": "testing",
                "file_overwrite": False,
            },
        },
    }
    uploads = storages["uploads"]

    def work() -> None:
        uploads.save("invoices/7.pdf", ContentFile(b"%PDF"))
        uploads.exists("invoices/7.pdf")

    trace = traced(work)
    spans = {span.id: span for span in trace.spans}

    # Each storage call, its S3 requests inside it: the agent counts them as the storage call's, one N+1 once.
    assert [(span.kind, span.name, spans[str(span.parent_id)].name) for span in calls(trace)] == [
        ("storage", "storage save invoices/7.pdf", "GET /"),
        ("storage", "S3 HeadObject uploads/invoices/7.pdf", "storage save invoices/7.pdf"),
        ("storage", "S3 PutObject uploads/invoices/7.pdf", "storage save invoices/7.pdf"),
        ("storage", "storage exists invoices/7.pdf", "GET /"),
        ("storage", "S3 HeadObject uploads/invoices/7.pdf", "storage exists invoices/7.pdf"),
    ]


# ---------- DynamoDB ----------


@pytest.fixture
def users(client: Callable[[str], Any]) -> Any:  # noqa: ANN401 (a DynamoDB client)
    dynamodb = client("dynamodb")
    dynamodb.create_table(
        TableName="users",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    yield dynamodb
    dynamodb.delete_table(TableName="users")


def test_dynamodb_commands_are_datastore_spans(users: Any, endpoint: str, validator: Draft202012Validator) -> None:  # noqa: ANN401
    def work() -> None:
        users.put_item(TableName="users", Item={"id": {"S": "1"}, "name": {"S": "Ada"}})
        for user in ("1", "2", "1"):
            users.get_item(TableName="users", Key={"id": {"S": user}})

    trace = traced(work)
    validator.validate(trace.to_dict())
    put, *gets = calls(trace)
    get = '{"TableName": "users", "Key": {"id": "?"}}'

    assert [(span.kind, span.name) for span in calls(trace)] == [("dynamodb", "DynamoDB PutItem users")] + [
        ("dynamodb", "DynamoDB GetItem users")
    ] * 3

    assert put.attrs == {
        "rpc.system": "aws-api",
        "rpc.service": "DynamoDB",
        "rpc.method": "PutItem",
        "cloud.region": REGION,
        "db.system": "dynamodb",
        "db.operation": "PutItem",
        "db.collection.name": "users",
        "aws.dynamodb.table_names": ["users"],
        "db.statement": '{"TableName": "users", "Item": {"id": "?", "name": "?"}}',
        "db.params_hash": IsStr(regex=r"[0-9a-f]{12}"),
        "db.params": ["1", "Ada"],
        **where(endpoint),
        "code.origin": origin("work"),
        "aws.request_id": REQUEST_ID,
        "http.response.status_code": 200,
    }

    # The same query: told apart by its values, which stay out of the statement.
    assert [span.attrs["db.statement"] for span in gets] == [get, get, get]
    assert gets[0].attrs["db.params_hash"] == gets[2].attrs["db.params_hash"] != gets[1].attrs["db.params_hash"]
    # User 2 isn't there: no item.
    assert [span.attrs.get("db.response.returned_rows") for span in gets] == [1, None, 1]


def test_dynamodb_values_can_be_left_out(users: Any, settings: Settings) -> None:  # noqa: ANN401
    settings.TRACEPANEL = {"capture_sql_params": False}

    (span,) = calls(traced(lambda: users.get_item(TableName="users", Key={"id": {"S": "42"}})))

    assert "db.params" not in span.attrs
    assert span.attrs["db.params_hash"] == IsStr(regex=r"[0-9a-f]{12}")


def test_a_table_s_conditions_are_its_statement(users: Any, endpoint: str) -> None:  # noqa: ANN401
    table = boto3.resource("dynamodb", endpoint_url=endpoint, region_name=REGION, **CREDENTIALS).Table("users")

    (span,) = calls(traced(lambda: table.query(KeyConditionExpression=Key("id").eq("7"))))

    assert span.name == "DynamoDB Query users"

    assert span.attrs["db.statement"] == '{"TableName": "users", "KeyConditionExpression": "id = ?"}'
    assert span.attrs["db.response.returned_rows"] == 0


# ---------- SQS, SNS, the rest ----------


def test_messages_sent_are_messaging_spans(client: Callable[[str], Any], endpoint: str) -> None:
    sqs, sns = client("sqs"), client("sns")
    queue = sqs.create_queue(QueueName="emails")["QueueUrl"]
    topic = sns.create_topic(Name="orders")["TopicArn"]

    def work() -> None:
        sqs.send_message(QueueUrl=queue, MessageBody="hello")
        sqs.send_message_batch(
            QueueUrl=queue, Entries=[{"Id": "1", "MessageBody": "a"}, {"Id": "2", "MessageBody": "b"}]
        )
        sqs.receive_message(QueueUrl=queue)
        sns.publish(TopicArn=topic, Message="paid")

    trace = traced(work)
    send, batch, receive, publish = calls(trace)

    assert [(span.kind, span.name) for span in calls(trace)] == [
        ("sqs", "SQS SendMessage emails"),
        ("sqs", "SQS SendMessageBatch emails"),
        # Not a message sent: a call.
        ("rpc", "SQS ReceiveMessage"),
        ("messaging", "SNS Publish orders"),
    ]

    assert send.attrs == {
        "rpc.system": "aws-api",
        "rpc.service": "SQS",
        "rpc.method": "SendMessage",
        "cloud.region": REGION,
        "messaging.system": "aws_sqs",
        "messaging.operation": "publish",
        "aws.sqs.queue.url": queue,
        "messaging.destination.name": "emails",
        "messaging.operation.name": "SendMessage",
        **where(endpoint),
        "code.origin": origin("work"),
        "aws.request_id": REQUEST_ID,
        "http.response.status_code": 200,
        "messaging.message.id": IsStr(min_length=1),
    }

    assert batch.attrs["messaging.batch.message_count"] == 2

    assert receive.attrs["rpc.method"] == "ReceiveMessage"

    assert publish.attrs["messaging.system"] == "aws.sns"
    assert publish.attrs["aws.sns.topic.arn"] == topic


def test_a_body_streamed_from_a_file_has_no_size_until_aws_answers(client: Callable[[str], Any]) -> None:
    s3 = client("s3")
    s3.create_bucket(Bucket="logs", CreateBucketConfiguration={"LocationConstraint": REGION})

    (span,) = calls(traced(lambda: s3.put_object(Bucket="logs", Key="a.log", Body=io.BytesIO(b"line"))))

    assert "storage.bytes" not in span.attrs


# ---------- what the agent finds ----------


def test_the_agent_finds_a_loop_s_calls(users: Any, client: Callable[[str], Any]) -> None:  # noqa: ANN401
    s3, sqs = client("s3"), client("sqs")
    s3.create_bucket(Bucket="photos", CreateBucketConfiguration={"LocationConstraint": REGION})
    queue = sqs.create_queue(QueueName="thumbnails")["QueueUrl"]

    def per_item() -> None:
        for n in range(5):
            users.get_item(TableName="users", Key={"id": {"S": str(n)}})
            s3.put_object(Bucket="photos", Key=f"albums/{n}.jpg", Body=b"jpg")
            sqs.send_message(QueueUrl=queue, MessageBody=str(n))

    def twice() -> None:
        for _ in range(2):
            users.get_item(TableName="users", Key={"id": {"S": "1"}})

    found = issues(traced(per_item))

    # One issue each, on the AWS call: their HTTP requests aren't calls the agent counts again.
    assert [(issue.kind, issue.title) for issue in found] == [
        ("n+1", "N+1: 5 similar DynamoDB commands · GetItem users"),
        ("n+1", "N+1: 5 similar Amazon SQS messages · publish thumbnails"),
        ("n+1", "N+1: 5 similar storage calls · PutObject photos/albums/*"),
    ]

    assert [issue.hint for issue in found[:2]] == [IsStr(regex=".*BatchGetItem.*"), IsStr(regex=".*SendMessageBatch.*")]

    assert [(issue.kind, issue.title) for issue in issues(traced(twice))] == [
        ("duplicates", "Duplicate DynamoDB commands: 2 identical · GetItem users")
    ]
