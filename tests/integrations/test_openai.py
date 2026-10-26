import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from dirty_equals import IsFloat, IsInt, IsPositiveInt, IsStr
from jsonschema import Draft202012Validator

openai = pytest.importorskip("openai")

from tests.integrations.llmstub import KEY, Stub, serve  # noqa: E402 (optional test group)
from tracepanel.core import conf  # noqa: E402
from tracepanel.core.context import record_span, start_trace  # noqa: E402
from tracepanel.core.masking import MASK  # noqa: E402
from tracepanel.core.model import Span, Trace  # noqa: E402
from tracepanel.integrations import http_clients  # noqa: E402
from tracepanel.integrations import openai as traced_openai  # noqa: E402
from tracepanel.storage.transport import complete  # noqa: E402

http_clients.install()
traced_openai.install()

ORIGIN = IsStr(regex=r"tests/integrations/test_openai\.py:\d+ in test_\w+")
SERVER = {"server.address": "127.0.0.1", "server.port": IsInt()}
COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Hello there"}, "finish_reason": "stop"},
    ],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}
TOOL_CALLS = {
    **COMPLETION,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "log_in", "arguments": '{"user": "ann", "password": "hunter2"}'},
                    },
                ],
            },
            "finish_reason": "tool_calls",
        },
    ],
}
RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "created_at": 1,
    "status": "completed",
    "model": "gpt-5-2025-08-07",
    "output": [
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Hi", "annotations": list[object]()}],
        },
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search", "arguments": "{}"},
    ],
    "usage": {
        "input_tokens": 20,
        "output_tokens": 9,
        "total_tokens": 29,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 3},
    },
}
QUESTION = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}]


@pytest.fixture(scope="module")
def stub() -> Iterator[Stub]:
    yield from serve()


@pytest.fixture(autouse=True)
def fresh(stub: Stub) -> None:
    stub.reset()


def client(stub: Stub) -> Any:  # noqa: ANN401 (the SDK's client)
    return openai.OpenAI(api_key=KEY, base_url=f"{stub.url}/v1", max_retries=0)


def async_client(stub: Stub) -> Any:  # noqa: ANN401
    return openai.AsyncOpenAI(api_key=KEY, base_url=f"{stub.url}/v1", max_retries=0)


def calls(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if "gen_ai.operation.name" in span.attrs]


def http_calls(trace: Trace) -> list[Span]:
    return [span for span in trace.spans if span.kind == "http.client"]


def chunk(choice: dict[str, object] | None = None, **fields: object) -> dict[str, object]:
    return {
        "id": "chatcmpl-2",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, **choice}] if choice is not None else [],
        **fields,
    }


STREAM: list[tuple[str | None, object]] = [
    (None, chunk({"delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None})),
    (None, chunk({"delta": {"content": "lo"}, "finish_reason": None})),
    (
        None,
        chunk(
            {
                "delta": {
                    "tool_calls": [{"index": 0, "id": "call_9", "type": "function", "function": {"name": "lookup"}}],
                },
                "finish_reason": None,
            }
        ),
    ),
    (None, chunk({"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q": 1}'}}]}})),
    (None, chunk({"delta": {}, "finish_reason": "tool_calls"})),
    (None, chunk(None, usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})),
    (None, "[DONE]"),
]


# ---------- chat completions ----------


def test_a_chat_completion_is_a_span_with_its_http_call_inside(stub: Stub, validator: Draft202012Validator) -> None:
    stub.answer(COMPLETION)

    with start_trace("http", "POST /ask") as trace:
        client(stub).chat.completions.create(
            model="gpt-4o-mini", messages=QUESTION, max_tokens=100, temperature=0.2, stop="END"
        )
    complete(trace)

    validator.validate(trace.to_dict())

    [call] = calls(trace)
    assert call.to_dict() == {
        "id": call.id,
        "parent_id": trace.spans[0].id,
        "kind": "function",
        "name": "chat gpt-4o-mini",
        "start": IsPositiveInt,
        "duration": IsPositiveInt,
        "status": "ok",
        "attrs": {
            "gen_ai.provider.name": "openai",
            "gen_ai.operation.name": "chat",
            "openai.api.type": "chat_completions",
            "gen_ai.request.model": "gpt-4o-mini",
            "gen_ai.request.max_tokens": 100,
            "gen_ai.request.temperature": 0.2,
            "gen_ai.request.stop_sequences": ["END"],
            **SERVER,
            "code.origin": ORIGIN,
            "gen_ai.response.id": "chatcmpl-1",
            "gen_ai.response.model": "gpt-4o-mini-2024-07-18",
            "gen_ai.response.finish_reasons": ["stop"],
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": 5,
            "gen_ai.usage.cache_read.input_tokens": 4,
        },
    }

    [http] = http_calls(trace)
    assert (http.name, http.parent_id, http.attrs["http.response.status_code"]) == (
        "POST 127.0.0.1/v1/chat/completions",
        call.id,
        200,
    )

    # The API got the trace context of the HTTP call, and the request as the app made it.
    [(path, body, traceparent)] = stub.received
    assert (path, body["model"], traceparent) == ("/v1/chat/completions", "gpt-4o-mini", IsStr(regex=r"00-\w+-\w+-01"))


def test_the_tools_a_model_calls_are_named_and_counted(stub: Stub) -> None:
    stub.answer(TOOL_CALLS)

    with start_trace("http", "POST /ask") as trace:
        client(stub).chat.completions.create(model="gpt-4o-mini", messages=QUESTION, n=2)

    [call] = calls(trace)
    assert {key: value for key, value in call.attrs.items() if "tool" in key or "finish" in key or "choice" in key} == {
        "gen_ai.request.choice.count": 2,
        "gen_ai.response.finish_reasons": ["tool_calls"],
        "gen_ai.response.tool_calls": ["get_weather", "log_in"],
        "gen_ai.response.tool_call_count": 2,
    }

    # The messages are the app's users' words: kept only when asked for.
    assert not [key for key in call.attrs if key.endswith(".messages")]


def test_a_stream_lasts_until_it_is_read_and_counts_what_its_chunks_say(stub: Stub) -> None:
    stub.stream(STREAM)

    with start_trace("http", "POST /ask") as trace:
        stream = client(stub).chat.completions.create(
            model="gpt-4o-mini", messages=QUESTION, stream=True, stream_options={"include_usage": True}
        )
        # What the app does while it reads the stream isn't part of the call.
        record_span("function", "render", start=0, duration=0, attrs={})
        texts = [each.choices[0].delta.content for each in stream if each.choices]

    assert texts == ["Hel", "lo", None, None, None]

    [call] = calls(trace)
    assert call.status == "ok"
    assert call.attrs == {
        "gen_ai.provider.name": "openai",
        "gen_ai.operation.name": "chat",
        "openai.api.type": "chat_completions",
        "gen_ai.request.model": "gpt-4o-mini",
        "gen_ai.request.stream": True,
        **SERVER,
        "code.origin": ORIGIN,
        "gen_ai.response.time_to_first_chunk": IsFloat(gt=0),
        "gen_ai.response.id": "chatcmpl-2",
        "gen_ai.response.model": "gpt-4o-mini-2024-07-18",
        "gen_ai.response.finish_reasons": ["tool_calls"],
        "gen_ai.usage.input_tokens": 7,
        "gen_ai.usage.output_tokens": 3,
        "gen_ai.response.tool_calls": ["lookup"],
        "gen_ai.response.tool_call_count": 1,
    }

    [render] = [span for span in trace.spans if span.name == "render"]
    assert render.parent_id == trace.spans[0].id

    [http] = http_calls(trace)
    assert http.parent_id == call.id


def test_a_stream_closed_early_ends_there(stub: Stub) -> None:
    stub.stream(STREAM)

    with start_trace("http", "POST /ask") as trace:
        stream = client(stub).chat.completions.create(model="gpt-4o-mini", messages=QUESTION, stream=True)
        next(iter(stream))
        stream.close()

    [call] = calls(trace)
    assert (call.status, call.duration, call.attrs["gen_ai.response.finish_reasons"]) == (
        "ok",
        IsPositiveInt,
        ["error"],
    )


def test_async_calls_and_streams(stub: Stub) -> None:
    stub.answer(COMPLETION)
    stub.stream(STREAM)

    async def work() -> None:
        api = async_client(stub)
        await api.chat.completions.create(model="gpt-4o-mini", messages=QUESTION)
        stream = await api.chat.completions.create(model="gpt-4o-mini", messages=QUESTION, stream=True)
        async for _ in stream:
            pass

    with start_trace("http", "POST /ask") as trace:
        asyncio.run(work())

    assert [
        (span.name, span.attrs.get("gen_ai.request.stream"), span.attrs["gen_ai.response.finish_reasons"])
        for span in calls(trace)
    ] == [("chat gpt-4o-mini", None, ["stop"]), ("chat gpt-4o-mini", True, ["tool_calls"])]

    assert [span.parent_id for span in http_calls(trace)] == [span.id for span in calls(trace)]


# ---------- responses, embeddings ----------


def test_a_response_is_a_chat_call(stub: Stub) -> None:
    stub.answer(RESPONSE)

    with start_trace("http", "POST /ask") as trace:
        client(stub).responses.create(
            model="gpt-5", input="Hi", instructions="Be brief.", max_output_tokens=50, reasoning={"effort": "low"}
        )

    [call] = calls(trace)
    assert call.name == "chat gpt-5"
    assert call.attrs == {
        "gen_ai.provider.name": "openai",
        "gen_ai.operation.name": "chat",
        "openai.api.type": "responses",
        "gen_ai.request.model": "gpt-5",
        "gen_ai.request.max_tokens": 50,
        "gen_ai.request.reasoning.level": "low",
        **SERVER,
        "code.origin": ORIGIN,
        "gen_ai.response.id": "resp_1",
        "gen_ai.response.model": "gpt-5-2025-08-07",
        "gen_ai.response.finish_reasons": ["completed"],
        "gen_ai.usage.input_tokens": 20,
        "gen_ai.usage.output_tokens": 9,
        "gen_ai.usage.reasoning.output_tokens": 3,
        "gen_ai.response.tool_calls": ["search"],
        "gen_ai.response.tool_call_count": 1,
    }


def test_a_streamed_response_reads_the_response_its_last_event_carries(stub: Stub) -> None:
    created = {**RESPONSE, "status": "in_progress", "output": list[object](), "usage": None}
    stub.stream(
        [
            ("response.created", {"type": "response.created", "sequence_number": 0, "response": created}),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Hi",
                    "logprobs": [],
                },
            ),
            ("response.completed", {"type": "response.completed", "sequence_number": 2, "response": RESPONSE}),
        ]
    )

    with start_trace("http", "POST /ask") as trace:
        for _ in client(stub).responses.create(model="gpt-5", input="Hi", stream=True):
            pass

    [call] = calls(trace)
    assert {key: call.attrs[key] for key in call.attrs if key.startswith(("gen_ai.response", "gen_ai.usage"))} == {
        "gen_ai.response.time_to_first_chunk": IsFloat(gt=0),
        "gen_ai.response.id": "resp_1",
        "gen_ai.response.model": "gpt-5-2025-08-07",
        "gen_ai.response.finish_reasons": ["completed"],
        "gen_ai.usage.input_tokens": 20,
        "gen_ai.usage.output_tokens": 9,
        "gen_ai.usage.reasoning.output_tokens": 3,
        "gen_ai.response.tool_calls": ["search"],
        "gen_ai.response.tool_call_count": 1,
    }


def test_embeddings(stub: Stub) -> None:
    stub.answer(
        {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }
    )

    with start_trace("http", "POST /ask") as trace:
        client(stub).embeddings.create(
            model="text-embedding-3-small", input="Hi", dimensions=2, encoding_format="float"
        )

    [call] = calls(trace)
    assert (call.name, call.attrs) == (
        "embeddings text-embedding-3-small",
        {
            "gen_ai.provider.name": "openai",
            "gen_ai.operation.name": "embeddings",
            "gen_ai.request.model": "text-embedding-3-small",
            "gen_ai.request.encoding_formats": ["float"],
            "gen_ai.embeddings.dimension.count": 2,
            **SERVER,
            "code.origin": ORIGIN,
            "gen_ai.response.model": "text-embedding-3-small",
            "gen_ai.usage.input_tokens": 3,
        },
    )


# ---------- errors ----------


def test_a_rate_limit_is_an_error_with_its_status(stub: Stub) -> None:
    stub.answer({"error": {"message": "Slow down", "type": "requests", "code": "rate_limit_exceeded"}}, 429)

    with start_trace("http", "POST /ask") as trace, pytest.raises(openai.RateLimitError):
        client(stub).chat.completions.create(model="gpt-4o-mini", messages=QUESTION)

    [call] = calls(trace)
    assert (call.status, call.attrs["error.type"], call.attrs["http.response.status_code"]) == (
        "error",
        "RateLimitError",
        429,
    )


def test_a_retry_is_another_http_call_inside_the_same_call(stub: Stub) -> None:
    stub.answer({"error": {"message": "Slow down"}}, 429, retry_after_ms="1")
    stub.answer(COMPLETION)

    with start_trace("http", "POST /ask") as trace:
        openai.OpenAI(api_key=KEY, base_url=f"{stub.url}/v1", max_retries=1).chat.completions.create(
            model="gpt-4o-mini", messages=QUESTION
        )

    [call] = calls(trace)
    assert call.status == "ok"
    assert [(span.parent_id, span.attrs["http.response.status_code"]) for span in http_calls(trace)] == [
        (call.id, 429),
        (call.id, 200),
    ]


# ---------- content ----------


def test_the_messages_are_kept_when_asked_for_masked_and_cut(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_CAPTURE_LLM_CONTENT", "1")
    monkeypatch.setenv("TRACEPANEL_MAX_BODY_KB", "1")
    conf.reset()
    stub.answer(TOOL_CALLS)
    stub.answer(COMPLETION)

    with start_trace("http", "POST /ask") as trace:
        api = client(stub)
        api.chat.completions.create(model="gpt-4o-mini", messages=QUESTION)
        api.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "x" * 2000}])
    conf.reset()

    first, long = calls(trace)
    assert json.loads(str(first.attrs["gen_ai.input.messages"])) == [
        {"role": "system", "parts": [{"type": "text", "content": "Be brief."}]},
        {"role": "user", "parts": [{"type": "text", "content": "Hi"}]},
    ]
    assert json.loads(str(first.attrs["gen_ai.output.messages"])) == [
        {
            "role": "assistant",
            "parts": [
                {"type": "tool_call", "id": "call_1", "name": "get_weather", "arguments": {"city": "Oslo"}},
                {"type": "tool_call", "id": "call_2", "name": "log_in", "arguments": {"user": "ann", "password": MASK}},
            ],
            "finish_reason": "tool_calls",
        },
    ]

    assert (len(str(long.attrs["gen_ai.input.messages"])), long.attrs["gen_ai.input.messages.truncated"]) == (
        1024,
        True,
    )


def test_outside_a_trace_nothing_is_recorded(stub: Stub) -> None:
    stub.answer(COMPLETION)

    answer = client(stub).chat.completions.create(model="gpt-4o-mini", messages=QUESTION)

    assert answer.choices[0].message.content == "Hello there"
