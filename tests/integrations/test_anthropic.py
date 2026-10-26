import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from dirty_equals import IsFloat, IsInt, IsPositiveInt, IsStr
from jsonschema import Draft202012Validator

anthropic = pytest.importorskip("anthropic")

from tests.integrations.llmstub import KEY, Stub, serve  # noqa: E402 (optional test group)
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.context import start_trace  # noqa: E402
from tracepanel.core.masking import MASK  # noqa: E402
from tracepanel.core.model import Span, Trace  # noqa: E402
from tracepanel.integrations import anthropic as traced_anthropic  # noqa: E402
from tracepanel.integrations import http_clients  # noqa: E402
from tracepanel.storage.transport import complete  # noqa: E402

http_clients.install()
traced_anthropic.install()

ORIGIN = IsStr(regex=r"tests/integrations/test_anthropic\.py:\d+ in test_\w+")
SERVER = {"server.address": "127.0.0.1", "server.port": IsInt()}
MODEL = "claude-sonnet-5-5"
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5-20260901",
    "content": [
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Oslo", "api_key": "k"}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 10,
        "output_tokens": 6,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 20,
    },
}
QUESTION = [{"role": "user", "content": "Weather in Oslo?"}]
STREAM: list[tuple[str | None, object]] = [
    (
        "message_start",
        {
            "type": "message_start",
            "message": {
                **MESSAGE,
                "id": "msg_2",
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 9, "output_tokens": 1, "cache_read_input_tokens": 5},
            },
        },
    ),
    (
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ),
    ("ping", {"type": "ping"}),
    (
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Sun"}},
    ),
    (
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ny"}},
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_2", "name": "lookup", "input": {}},
        },
    ),
    (
        "content_block_delta",
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q": '}},
    ),
    (
        "content_block_delta",
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "1}"}},
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 4},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]


@pytest.fixture(scope="module")
def stub() -> Iterator[Stub]:
    yield from serve()


@pytest.fixture(autouse=True)
def fresh(stub: Stub) -> None:
    stub.reset()


def client(stub: Stub) -> Any:  # noqa: ANN401 (the SDK's client)
    return anthropic.Anthropic(api_key=KEY, base_url=stub.url, max_retries=0)


def calls(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if "gen_ai.operation.name" in span.attrs]


def http_calls(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "http.client"]


def test_a_message_is_a_span_with_its_http_call_inside(stub: Stub, validator: Draft202012Validator) -> None:
    stub.answer(MESSAGE)

    with start_trace("http", "POST /ask") as trace:
        client(stub).messages.create(
            model=MODEL, max_tokens=256, messages=QUESTION, stop_sequences=["END"], output_config={"effort": "high"}
        )
    complete(trace)

    validator.validate(trace.to_dict())

    [call] = calls(trace)
    assert call.to_dict() == {
        "id": call.id,
        "parent_id": trace.spans[0].id,
        "kind": "function",
        "name": "chat claude-sonnet-5-5",
        "start": IsPositiveInt,
        "duration": IsPositiveInt,
        "status": "ok",
        "attrs": {
            "gen_ai.provider.name": "anthropic",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": MODEL,
            "gen_ai.request.max_tokens": 256,
            "gen_ai.request.stop_sequences": ["END"],
            "gen_ai.request.reasoning.level": "high",
            **SERVER,
            "code.origin": ORIGIN,
            "gen_ai.response.id": "msg_1",
            "gen_ai.response.model": "claude-sonnet-5-5-20260901",
            "gen_ai.response.finish_reasons": ["tool_use"],
            # The cache's tokens are part of the input, as the conventions count it.
            "gen_ai.usage.input_tokens": 130,
            "gen_ai.usage.output_tokens": 6,
            "gen_ai.usage.cache_read.input_tokens": 100,
            "gen_ai.usage.cache_write.input_tokens": 20,
            "gen_ai.response.tool_calls": ["get_weather"],
            "gen_ai.response.tool_call_count": 1,
        },
    }

    [http] = http_calls(trace)
    assert (http.name, http.parent_id) == ("POST 127.0.0.1/v1/messages", call.id)


def test_the_stream_helper_is_followed_to_its_end(stub: Stub) -> None:
    stub.stream(STREAM)

    with start_trace("http", "POST /ask") as trace:
        with client(stub).messages.stream(model=MODEL, max_tokens=256, messages=QUESTION) as stream:
            texts = list(stream.text_stream)
        final = stream.get_final_message()

    assert ("".join(texts), final.stop_reason) == ("Sunny", "end_turn")

    [call] = calls(trace)
    assert call.status == "ok"
    assert {key: call.attrs[key] for key in call.attrs if key.startswith(("gen_ai.response", "gen_ai.usage"))} == {
        "gen_ai.response.time_to_first_chunk": IsFloat(gt=0),
        "gen_ai.response.id": "msg_2",
        "gen_ai.response.model": "claude-sonnet-5-5-20260901",
        "gen_ai.response.finish_reasons": ["end_turn"],
        "gen_ai.usage.input_tokens": 14,
        "gen_ai.usage.output_tokens": 4,
        "gen_ai.usage.cache_read.input_tokens": 5,
        "gen_ai.response.tool_calls": ["lookup"],
        "gen_ai.response.tool_call_count": 1,
    }
    assert call.attrs["gen_ai.request.stream"] is True

    [http] = http_calls(trace)
    assert http.parent_id == call.id


def test_async_calls_and_streams(stub: Stub) -> None:
    stub.answer(MESSAGE)
    stub.stream(STREAM)

    async def work() -> None:
        api = anthropic.AsyncAnthropic(api_key=KEY, base_url=stub.url, max_retries=0)
        await api.messages.create(model=MODEL, max_tokens=256, messages=QUESTION)
        stream = await api.messages.create(model=MODEL, max_tokens=256, messages=QUESTION, stream=True)
        async for _ in stream:
            pass

    with start_trace("http", "POST /ask") as trace:
        asyncio.run(work())

    assert [
        (span.attrs.get("gen_ai.request.stream"), span.attrs["gen_ai.response.finish_reasons"]) for span in calls(trace)
    ] == [
        (None, ["tool_use"]),
        (True, ["end_turn"]),
    ]

    assert [span.parent_id for span in http_calls(trace)] == [span.id for span in calls(trace)]


def test_an_overloaded_api_is_an_error_with_its_status(stub: Stub) -> None:
    stub.answer({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, 529)

    with start_trace("http", "POST /ask") as trace, pytest.raises(anthropic.OverloadedError):
        client(stub).messages.create(model=MODEL, max_tokens=256, messages=QUESTION)

    [call] = calls(trace)
    assert (call.status, call.attrs["error.type"], call.attrs["http.response.status_code"]) == (
        "error",
        "OverloadedError",
        529,
    )


def test_an_error_in_the_stream_is_the_calls_error(stub: Stub) -> None:
    stub.stream(
        [
            STREAM[0],
            ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
        ]
    )

    with start_trace("http", "POST /ask") as trace, pytest.raises(anthropic.APIStatusError):
        for _ in client(stub).messages.create(model=MODEL, max_tokens=256, messages=QUESTION, stream=True):
            pass

    [call] = calls(trace)
    assert (call.status, call.attrs["error.type"], call.attrs["gen_ai.response.finish_reasons"]) == (
        "error",
        IsStr(regex=r"\w+Error"),
        ["error"],
    )


def test_the_messages_are_kept_when_asked_for_and_masked(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_CAPTURE_LLM_CONTENT", "1")
    conf.reset()
    stub.answer(MESSAGE)
    stub.stream(STREAM)

    with start_trace("http", "POST /ask") as trace:
        api = client(stub)
        api.messages.create(model=MODEL, max_tokens=256, messages=QUESTION, system="Be brief.")
        with api.messages.stream(model=MODEL, max_tokens=256, messages=QUESTION) as stream:
            stream.until_done()
    conf.reset()

    created, streamed = calls(trace)
    assert json.loads(str(created.attrs["gen_ai.system_instructions"])) == [{"type": "text", "content": "Be brief."}]
    assert json.loads(str(created.attrs["gen_ai.input.messages"])) == [
        {"role": "user", "parts": [{"type": "text", "content": "Weather in Oslo?"}]},
    ]
    assert json.loads(str(created.attrs["gen_ai.output.messages"])) == [
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "Let me look."},
                {
                    "type": "tool_call",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "arguments": {"city": "Oslo", "api_key": MASK},
                },
            ],
            "finish_reason": "tool_use",
        },
    ]

    assert json.loads(str(streamed.attrs["gen_ai.output.messages"])) == [
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "Sunny"},
                {"type": "tool_call", "id": "toolu_2", "name": "lookup", "arguments": {"q": 1}},
            ],
            "finish_reason": "end_turn",
        },
    ]
