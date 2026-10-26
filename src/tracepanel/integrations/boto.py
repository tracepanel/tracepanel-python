"""AWS: every call boto3 makes through botocore, and aiobotocore's async clients, as a span: `S3 PutObject bucket/key`.

A call is the operation the app asked for, wrapped where every client method goes (the client's _make_api_call): its
service and operation (`rpc.*`, as OpenTelemetry's AWS SDK conventions have them), the region, the request id AWS gave
it, how many times botocore retried it and, when it failed, the error's code (`NoSuchKey`). Its HTTP requests are part
of it, retries included, not calls of their own (tracepanel.integrations.http_clients).

Its kind says what the call does for the app, and the agent analyzes it with its peers:
- S3: a `storage` span, `storage.system` s3 with the bucket and key as its path, as a Django storage's call is.
- DynamoDB: a `dynamodb` span, a datastore's command. As with MongoDB its request's shape is `db.statement`, with the
  item's values as `?`, and the values are `db.params` (with `capture_sql_params`) and `db.params_hash`: the agent tells
  a loop's GetItem per key (N+1) from the same GetItem again (duplicates) by them.
- SQS's SendMessage and SNS's Publish (their batches too): a message sent, `sqs` and `messaging` spans.
- Any other call (SQS's ReceiveMessage, Lambda, Secrets Manager…): an `rpc` span, `rpc.system` aws-api.

A call made for a Django storage, a cache or a task queue (django-storages' S3Storage, a broker on SQS) is a span of its
own kind inside that call's span: the agent counts it as the layer's, so one loop is not counted twice. upload_file(),
download_file() and their fileobj kin send their requests from s3transfer's threads: those carry the trace there, and
the requests are spans where the transfer started.
"""

import contextvars
import functools
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from tracepanel.core import conf, sql
from tracepanel.core.context import blocking_attrs, current_trace, start_span
from tracepanel.core.frames import origin
from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs, Scalar
from tracepanel.core.patching import patch
from tracepanel.integrations import http_clients

SENDS = {"sqs": ("SendMessage", "SendMessageBatch"), "sns": ("Publish", "PublishBatch")}
# Where a DynamoDB request carries the app's values (an item, a key, an expression's values): these are `?`.
VALUES = frozenset({"Item", "Key", "Keys", "ExclusiveStartKey", "ExpressionAttributeValues", "Parameters"})
# A DynamoDB attribute value as the low-level client sends it: {"S": "alice"}.
TYPES = frozenset({"S", "N", "B", "BOOL", "NULL", "SS", "NS", "BS", "L", "M"})
MAX_STATEMENT = 2000
MAX_PARAMS = 200


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------- S3 ----------


def _s3(client: Any, operation: str, params: Mapping[str, Any]) -> tuple[str | None, Attrs]:  # noqa: ANN401
    bucket, key = _text(params.get("Bucket")), _text(params.get("Key"))
    # A listing's path is the prefix it lists.
    inside = key or _text(params.get("Prefix"))
    path = f"{bucket}/{inside}" if bucket and inside else bucket
    body = params.get("Body")
    size = len(body) if isinstance(body, (bytes, bytearray, str)) else _int(params.get("ContentLength"))
    kind = type(client)
    attrs: Attrs = {
        "storage.backend": f"{kind.__module__}.{kind.__qualname__}",
        "storage.system": "s3",
        "storage.op": operation,
        "storage.path": path,
        "aws.s3.bucket": bucket,
        "aws.s3.key": key,
        "aws.s3.copy_source": _copy_source(params.get("CopySource")),
        "aws.s3.upload_id": _text(params.get("UploadId")),
        "aws.s3.part_number": _int(params.get("PartNumber")),
        "storage.bytes": size,
    }
    return path, attrs


def _copy_source(source: object) -> str | None:
    if isinstance(source, Mapping):
        bucket, key = _text(source.get("Bucket")), _text(source.get("Key"))
        return f"{bucket}/{key}" if bucket and key else None
    return _text(source)


# ---------- DynamoDB ----------


def _tables(params: Mapping[str, Any]) -> list[str]:
    """The tables a request works on: its TableName, a batch's RequestItems, a transaction's items."""
    table = _text(params.get("TableName"))
    if table:
        return [table]
    items = params.get("RequestItems")
    if isinstance(items, Mapping):
        return [str(name) for name in items]
    found: list[str] = []
    for item in params.get("TransactItems") or ():
        for action in item.values() if isinstance(item, Mapping) else ():
            name = _text(action.get("TableName")) if isinstance(action, Mapping) else None
            if name and name not in found:
                found.append(name)
    return found


def _scalar(value: object) -> Scalar:
    return value if value is None or isinstance(value, (bool, int, float, str)) else str(value)


def _hide(value: object, values: list[Scalar]) -> object:
    """An item's or a key's values as `?` (collected in order), its attribute names kept."""
    if isinstance(value, Mapping):
        if len(value) == 1 and next(iter(value)) in TYPES:
            values.append(_scalar(next(iter(value.values()))))
            return "?"
        return {str(name): _hide(item, values) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        if all(not isinstance(item, (Mapping, list, tuple)) for item in value):
            values.extend(_scalar(item) for item in value)
            return ["?"]
        return [_hide(item, values) for item in value]
    values.append(_scalar(value))
    return "?"


def _condition(condition: Any, values: list[Scalar]) -> str:  # noqa: ANN401 (a boto3 condition: Key("id").eq(1))
    """A condition boto3's Table builds (`Key("id").eq(1)`) as the expression it sends, its values as `?`."""
    from boto3.dynamodb.conditions import ConditionExpressionBuilder  # noqa: PLC0415 (the app uses boto3's Table)

    built = ConditionExpressionBuilder().build_expression(condition)
    names, given = built.attribute_name_placeholders, built.attribute_value_placeholders

    def placeholder(found: re.Match[str]) -> str:
        if found[0] in names:
            return str(names[found[0]])
        values.append(_scalar(given.get(found[0])))
        return "?"

    return re.sub(r"#n\d+|:v\d+", placeholder, built.condition_expression)


def _shape(value: object, values: list[Scalar]) -> object:
    if type(value).__module__ == "boto3.dynamodb.conditions" and hasattr(value, "get_expression"):
        return _condition(value, values)
    if isinstance(value, Mapping):
        return {
            str(name): _hide(item, values) if name in VALUES else _shape(item, values) for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_shape(item, values) for item in value]
    return value


def _dynamodb(operation: str, params: Mapping[str, Any]) -> tuple[str | None, Attrs]:
    tables = _tables(params)
    names: list[Scalar] = list(tables)
    values: list[Scalar] = []
    text = json.dumps(_shape(params, values), default=str, separators=(", ", ": "))
    attrs: Attrs = {
        "db.system": "dynamodb",
        "db.operation": operation,
        "db.collection.name": tables[0] if len(tables) == 1 else None,
        "aws.dynamodb.table_names": names or None,
        "aws.dynamodb.index_name": _text(params.get("IndexName")),
        "db.statement": text if len(text) <= MAX_STATEMENT else f"{text[: MAX_STATEMENT - 1]}…",
        "db.params_hash": sql.params_hash(tuple(values)),
    }
    if values and len(values) <= MAX_PARAMS and conf.get_config().capture_sql_params:
        attrs["db.params"] = values
    return ", ".join(tables) or None, attrs


def _dynamodb_answer(response: Mapping[str, Any]) -> Attrs:
    """How many items a query, a scan or a get returned."""
    count = _int(response.get("Count"))
    if count is None and "Item" in response:
        count = 1
    return {} if count is None else {"db.response.returned_rows": count}


# ---------- SQS, SNS ----------


def _queue(url: object) -> str | None:
    """A queue's name: the last part of its URL."""
    text = _text(url)
    return (text.rstrip("/").rpartition("/")[2] or None) if text else None


def _message(service: str, operation: str, params: Mapping[str, Any]) -> tuple[str | None, Attrs]:
    entries = params.get("Entries", params.get("PublishBatchRequestEntries"))
    attrs: Attrs = {"messaging.system": "aws_sqs" if service == "sqs" else "aws.sns", "messaging.operation": "publish"}
    if service == "sqs":
        destination = _queue(params.get("QueueUrl"))
        attrs["aws.sqs.queue.url"] = _text(params.get("QueueUrl"))
    else:
        arn = _text(params.get("TopicArn")) or _text(params.get("TargetArn"))
        destination = arn.rpartition(":")[2] if arn else "phone_number" if params.get("PhoneNumber") else None
        attrs["aws.sns.topic.arn"] = _text(params.get("TopicArn"))
    attrs["messaging.destination.name"] = destination
    attrs["messaging.operation.name"] = operation
    attrs["messaging.batch.message_count"] = len(entries) if isinstance(entries, list) else None
    return destination, attrs


# ---------- every call ----------


def _service(client: Any) -> tuple[str, str]:  # noqa: ANN401 (a botocore client)
    """The service's endpoint prefix (`s3`, `dynamodb`) and its id as AWS writes it (`S3`, `DynamoDB`)."""
    model = client.meta.service_model
    return str(model.service_name), str(model.service_id)


def _start(client: Any, operation: str, params: Mapping[str, Any]) -> tuple[str, str, Attrs]:  # noqa: ANN401
    """The span's kind, name and attributes, from the call the app makes."""
    service, service_id = _service(client)
    meta = client.meta
    where = urlsplit(str(meta.endpoint_url or ""))
    attrs: Attrs = {
        "rpc.system": "aws-api",
        "rpc.service": service_id,
        "rpc.method": operation,
        "cloud.region": _text(meta.region_name),
    }
    kind, target = "rpc", None
    if service == "s3":
        kind, (target, specific) = "storage", _s3(client, operation, params)
        attrs.update(specific)
    elif service == "dynamodb":
        kind, (target, specific) = "dynamodb", _dynamodb(operation, params)
        attrs.update(specific)
    elif operation in SENDS.get(service, ()):
        kind, (target, specific) = ("sqs" if service == "sqs" else "messaging"), _message(service, operation, params)
        attrs.update(specific)
    attrs["server.address"] = where.hostname
    attrs["server.port"] = (where.port or (443 if where.scheme == "https" else 80)) if where.hostname else None
    attrs["code.origin"] = origin()
    attrs.update(blocking_attrs())
    name = f"{service_id} {operation} {target}" if target else f"{service_id} {operation}"
    return kind, name, {key: value for key, value in attrs.items() if value is not None}


def _answer(kind: str, response: object) -> Attrs:
    """What AWS answered: its request ids, the HTTP status, botocore's retries, and what the operation returned."""
    if not isinstance(response, Mapping):
        return {}
    given = response.get("ResponseMetadata")
    meta: Mapping[str, Any] = given if isinstance(given, Mapping) else {}
    attrs: Attrs = {
        "aws.request_id": _text(meta.get("RequestId")),
        "aws.extended_request_id": _text(meta.get("HostId")),
        "http.response.status_code": _int(meta.get("HTTPStatusCode")),
        "aws.retry_attempts": _int(meta.get("RetryAttempts")) or None,
    }
    if kind == "storage" and "ContentLength" in response:
        attrs["storage.bytes"] = _int(response.get("ContentLength"))
    elif kind == "dynamodb":
        attrs.update(_dynamodb_answer(response))
    elif kind in {"sqs", "messaging"}:
        attrs["messaging.message.id"] = _text(response.get("MessageId"))
    return {name: value for name, value in attrs.items() if value is not None}


def _failed(error: BaseException) -> Attrs:
    """A ClientError's code (`NoSuchKey`, `ConditionalCheckFailedException`) and AWS's answer; else the exception."""
    response = getattr(error, "response", None)
    found = response.get("Error") if isinstance(response, Mapping) else None
    code = found.get("Code") if isinstance(found, Mapping) else None
    return {**_answer("", response), "error.type": str(code) if code else type(error).__name__}


def _wrap(call: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(call)
    def traced(self: Any, operation_name: str, api_params: Mapping[str, Any]) -> Any:  # noqa: ANN401 (botocore's)
        if current_trace() is None:
            return call(self, operation_name, api_params)
        kind, name, attrs = _start(self, str(operation_name), api_params)
        token = http_clients.covered.set(True)
        try:
            with start_span(kind, name, attrs) as span:
                try:
                    response = call(self, operation_name, api_params)
                except Exception as exc:
                    if span is not None:
                        span.attrs.update(_failed(exc))
                    raise
                if span is not None:
                    span.attrs.update(_answer(kind, response))
                return response
        finally:
            http_clients.covered.reset(token)

    return traced


def _wrap_async(call: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(call)
    async def traced(self: Any, operation_name: str, api_params: Mapping[str, Any]) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return await call(self, operation_name, api_params)
        kind, name, attrs = _start(self, str(operation_name), api_params)
        token = http_clients.covered.set(True)
        try:
            with start_span(kind, name, attrs) as span:
                try:
                    response = await call(self, operation_name, api_params)
                except Exception as exc:
                    if span is not None:
                        span.attrs.update(_failed(exc))
                    raise
                if span is not None:
                    span.attrs.update(_answer(kind, response))
                return response
        finally:
            http_clients.covered.reset(token)

    return traced


class _InContext:
    """An s3transfer task that runs in the context it was submitted from: the trace, and the span it was started in."""

    def __init__(self, task: Any, context: contextvars.Context) -> None:  # noqa: ANN401 (s3transfer's Task)
        self._task, self._context = task, context

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 (the task's own: transfer_id…)
        return getattr(self._task, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self._context.run(self._task, *args, **kwargs)


def _s3transfer(s3transfer: ModuleType) -> None:
    del s3transfer
    from s3transfer.futures import BoundedExecutor  # noqa: PLC0415 (optional dependency, imported now)

    submit = BoundedExecutor.submit

    @functools.wraps(submit)
    def traced(self: Any, task: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (s3transfer's)
        if current_trace() is not None:
            task = _InContext(task, contextvars.copy_context())
        return submit(self, task, *args, **kwargs)

    patch(BoundedExecutor, "submit", traced)


def _botocore(botocore: ModuleType) -> None:
    del botocore
    from botocore.client import BaseClient  # noqa: PLC0415 (optional dependency, imported now)

    # Where every client method goes.
    patch(BaseClient, "_make_api_call", _wrap(vars(BaseClient)["_make_api_call"]))


def _aiobotocore(aiobotocore: ModuleType) -> None:
    del aiobotocore
    from aiobotocore.client import AioBaseClient  # noqa: PLC0415

    patch(AioBaseClient, "_make_api_call", _wrap_async(vars(AioBaseClient)["_make_api_call"]))


def install() -> None:
    """Wrap botocore's and aiobotocore's clients, and carry the trace into s3transfer's threads, each once imported."""
    when_imported("botocore", _botocore)
    when_imported("aiobotocore", _aiobotocore)
    when_imported("s3transfer", _s3transfer)
