"""OpenAI's SDK: chat completions, responses and embeddings as LLM call spans, sync and async, streamed or not.

`chat gpt-5` for a chat completion (`openai.api.type` `chat_completions`) and for a response (`responses`),
`embeddings text-embedding-3-small` for embeddings: see tracepanel.integrations._llm for what a span holds. The helpers
built on these endpoints (`.parse()`, `.stream()`) are the same calls. Azure OpenAI's client is `azure.ai.openai`.

A streamed chat completion counts its tokens only when the request asks for them (`stream_options={"include_usage":
True}`): OpenAI sends no usage otherwise.
"""

import importlib
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs
from tracepanel.integrations import _llm
from tracepanel.integrations._llm import Endpoint, Provider, Reply, Usage, get, items, settings, text, tool_call

_CHAT_SETTINGS = {
    "max_tokens": "max_tokens",
    "max_completion_tokens": "max_tokens",
    "temperature": "temperature",
    "top_p": "top_p",
    "frequency_penalty": "frequency_penalty",
    "presence_penalty": "presence_penalty",
    "seed": "seed",
    "stop": "stop_sequences",
    "reasoning_effort": "reasoning.level",
}
_RESPONSES_SETTINGS = {
    "max_output_tokens": "max_tokens",
    "temperature": "temperature",
    "top_p": "top_p",
    "previous_response_id": "previous_response.id",
}
_TEXT_PARTS = frozenset({"text", "input_text", "output_text"})


def _name(client: object) -> str:
    return "azure.ai.openai" if "Azure" in type(client).__name__ else "openai"


def _model(body: Mapping[str, Any]) -> Attrs:
    model = body.get("model")
    return {"gen_ai.request.model": model} if isinstance(model, str) else {}


def _parts(value: object) -> list[object]:
    """A message's content as the conventions' parts: its text, and the type of what else it holds (an image)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [text(value)]
    parts: list[object] = []
    for part in items(value):
        kind = get(part, "type")
        if kind in _TEXT_PARTS:
            parts.append(text(get(part, "text")))
        elif kind == "refusal":
            parts.append(text(get(part, "refusal")))
        else:
            parts.append({"type": kind})
    return parts


def _usage(usage: object, input_name: str, output_name: str) -> Usage:
    if usage is None:
        return Usage()
    input_details = get(usage, f"{input_name}_details")
    output_details = get(usage, f"{output_name}_details")
    return Usage(
        input=get(usage, input_name),
        output=get(usage, output_name),
        cache_read=get(input_details, "cached_tokens"),
        reasoning=get(output_details, "reasoning_tokens"),
    )


# ---------- chat completions ----------


def _chat_request(body: Mapping[str, Any]) -> Attrs:
    attrs = {**_model(body), **settings(body, _CHAT_SETTINGS)}
    choices = body.get("n")
    if isinstance(choices, int) and choices != 1:
        attrs["gen_ai.request.choice.count"] = choices
    return attrs


def _chat_message(message: object) -> dict[str, object]:
    role = get(message, "role")
    if role == "tool":
        return {
            "role": "tool",
            "parts": [
                {"type": "tool_call_response", "id": get(message, "tool_call_id"), "response": get(message, "content")}
            ],
        }
    parts = _parts(get(message, "content"))
    for call in items(get(message, "tool_calls")):
        function = get(call, "function")
        parts.append(tool_call(get(call, "id"), get(function, "name"), get(function, "arguments")))
    return {"role": role, "parts": parts}


def _chat_messages(body: Mapping[str, Any]) -> Attrs:
    return _llm.content("gen_ai.input.messages", [_chat_message(message) for message in items(body.get("messages"))])


def _chat_reply(result: object, capture: bool) -> Reply | None:  # noqa: FBT001 (the endpoint's callback)
    choices = get(result, "choices")
    if choices is None:
        return None
    reply = Reply(
        id=get(result, "id"),
        model=get(result, "model"),
        usage=_usage(get(result, "usage"), "prompt_tokens", "completion_tokens"),
    )
    messages: list[object] = []
    for choice in items(choices):
        reason = get(choice, "finish_reason")
        reply.finish_reasons.append(str(reason) if reason is not None else "error")
        message = get(choice, "message")
        reply.tools += [str(get(get(call, "function"), "name")) for call in items(get(message, "tool_calls"))]
        if capture:
            messages.append({**_chat_message(message), "role": "assistant", "finish_reason": reason})
    if capture:
        reply.messages = messages
    return reply


class _ChatChunks:
    """A streamed chat completion: each choice's text and tool calls, its finish reason, the usage at the end."""

    def __init__(self, capture: bool) -> None:  # noqa: FBT001
        self.capture = capture
        self.reply_ = Reply()
        # Each choice by its index: its text, its tool calls by their index ([id, name, arguments]), its finish reason.
        self.choices: dict[int, tuple[list[str], dict[int, list[str]], list[str]]] = {}

    def add(self, chunk: Any) -> None:  # noqa: ANN401 (a ChatCompletionChunk)
        self.reply_.id = self.reply_.id or get(chunk, "id")
        self.reply_.model = self.reply_.model or get(chunk, "model")
        usage = get(chunk, "usage")
        if usage is not None:
            self.reply_.usage = _usage(usage, "prompt_tokens", "completion_tokens")
        for choice in items(get(chunk, "choices")):
            texts, calls, reasons = self.choices.setdefault(get(choice, "index") or 0, ([], {}, []))
            delta = get(choice, "delta")
            piece = get(delta, "content")
            if self.capture and isinstance(piece, str):
                texts.append(piece)
            for call in items(get(delta, "tool_calls")):
                known = calls.setdefault(get(call, "index") or 0, ["", "", ""])
                function = get(call, "function")
                known[0] = get(call, "id") or known[0]
                known[1] = get(function, "name") or known[1]
                if self.capture:
                    known[2] += get(function, "arguments") or ""
            reason = get(choice, "finish_reason")
            if reason is not None:
                reasons.append(str(reason))

    def reply(self) -> Reply:
        reply = self.reply_
        messages: list[object] = []
        for index in sorted(self.choices):
            texts, calls, reasons = self.choices[index]
            reason = reasons[-1] if reasons else "error"
            reply.finish_reasons.append(reason)
            reply.tools += [calls[call][1] for call in sorted(calls)]
            if self.capture:
                parts: list[object] = [text("".join(texts))] if texts else []
                parts += [tool_call(*calls[call]) for call in sorted(calls)]
                messages.append({"role": "assistant", "parts": parts, "finish_reason": reason})
        if self.capture:
            reply.messages = messages
        return reply


# ---------- responses ----------


def _responses_request(body: Mapping[str, Any]) -> Attrs:
    attrs = {**_model(body), **settings(body, _RESPONSES_SETTINGS)}
    effort = get(body.get("reasoning"), "effort")
    if isinstance(effort, str):
        attrs["gen_ai.request.reasoning.level"] = effort
    return attrs


def _response_item(item: object) -> dict[str, object]:
    """An input item of a response as a message: a message itself, a tool's call or its output."""
    kind = get(item, "type")
    if kind == "function_call":
        return {
            "role": "assistant",
            "parts": [tool_call(get(item, "call_id"), get(item, "name"), get(item, "arguments"))],
        }
    if kind == "function_call_output":
        return {
            "role": "tool",
            "parts": [{"type": "tool_call_response", "id": get(item, "call_id"), "response": get(item, "output")}],
        }
    role = get(item, "role")
    if role is not None:
        return {"role": role, "parts": _parts(get(item, "content"))}
    return {"role": "user", "parts": [{"type": kind}]}


def _responses_messages(body: Mapping[str, Any]) -> Attrs:
    given = body.get("input")
    messages = (
        [{"role": "user", "parts": [text(given)]}]
        if isinstance(given, str)
        else list(map(_response_item, items(given)))
    )
    attrs = _llm.content("gen_ai.input.messages", messages)
    instructions = body.get("instructions")
    if isinstance(instructions, str):
        attrs.update(_llm.content("gen_ai.system_instructions", [text(instructions)]))
    return attrs


def _response_reply(result: object, capture: bool) -> Reply | None:  # noqa: FBT001
    output = get(result, "output")
    if output is None:
        return None
    incomplete = get(get(result, "incomplete_details"), "reason")
    status = get(result, "status")
    reason = str(incomplete or status or "error")
    reply = Reply(
        id=get(result, "id"),
        model=get(result, "model"),
        finish_reasons=[reason],
        usage=_usage(get(result, "usage"), "input_tokens", "output_tokens"),
    )
    parts: list[object] = []
    for item in items(output):
        kind = get(item, "type")
        if kind in {"function_call", "custom_tool_call"}:
            reply.tools.append(str(get(item, "name")))
            parts.append(
                tool_call(get(item, "call_id"), get(item, "name"), get(item, "arguments") or get(item, "input"))
            )
        elif kind == "message":
            parts += _parts(get(item, "content"))
        elif kind is not None:
            parts.append({"type": kind})
    if capture:
        reply.messages = [{"role": "assistant", "parts": parts, "finish_reason": reason}]
    return reply


class _ResponseChunks:
    """A streamed response: its events end with the whole response (completed, incomplete or failed)."""

    def __init__(self, capture: bool) -> None:  # noqa: FBT001
        self.capture = capture
        self.response: object = None

    def add(self, chunk: Any) -> None:  # noqa: ANN401 (a ResponseStreamEvent)
        response = get(chunk, "response")
        if response is not None:
            self.response = response

    def reply(self) -> Reply:
        found = _response_reply(self.response, self.capture) if self.response is not None else None
        return found or Reply(finish_reasons=["error"])


# ---------- embeddings ----------


def _embeddings_request(body: Mapping[str, Any]) -> Attrs:
    attrs = _model(body)
    encoding = body.get("encoding_format")
    if isinstance(encoding, str):
        attrs["gen_ai.request.encoding_formats"] = [encoding]
    dimensions = body.get("dimensions")
    if isinstance(dimensions, int):
        attrs["gen_ai.embeddings.dimension.count"] = dimensions
    return attrs


def _embeddings_reply(result: object, capture: bool) -> Reply | None:  # noqa: FBT001
    del capture
    if get(result, "data") is None:
        return None
    usage = get(result, "usage")
    return Reply(model=get(result, "model"), usage=Usage(input=get(usage, "prompt_tokens")))


PROVIDER = Provider(
    name=_name,
    endpoints={
        "POST /chat/completions": Endpoint("chat", _chat_request, _chat_reply, _ChatChunks, _chat_messages),
        "POST /responses": Endpoint("chat", _responses_request, _response_reply, _ResponseChunks, _responses_messages),
        "POST /embeddings": Endpoint("embeddings", _embeddings_request, _embeddings_reply),
    },
    extra={
        "/chat/completions": {"openai.api.type": "chat_completions"},
        "/responses": {"openai.api.type": "responses"},
    },
)


def _patch(module: ModuleType) -> None:
    del module
    client = importlib.import_module("openai._base_client")
    streaming = importlib.import_module("openai._streaming")
    _llm.wrap(PROVIDER, client.SyncAPIClient, [streaming.Stream], asynchronous=False)
    _llm.wrap(PROVIDER, client.AsyncAPIClient, [streaming.AsyncStream], asynchronous=True)
    _llm.wrap_close(streaming.Stream, asynchronous=False)
    _llm.wrap_close(streaming.AsyncStream, asynchronous=True)


def install() -> None:
    """Wrap OpenAI's clients once the app imports the SDK."""
    when_imported("openai", _patch)
