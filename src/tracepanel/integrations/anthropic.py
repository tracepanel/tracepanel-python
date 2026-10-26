"""Anthropic's SDK: messages as LLM call spans, sync and async, streamed or not.

`chat claude-sonnet-5-5` for each message created, through `messages.create()` or `messages.stream()` (the beta
messages too): see tracepanel.integrations._llm for what a span holds. The input tokens are counted as the conventions
have them, the cache's included (Anthropic counts them apart). The clients for Amazon Bedrock and Google Vertex AI are
`aws.bedrock` and `gcp.vertex_ai`.

The SDK makes an OpenTelemetry span of its own for each call when the app has a tracer provider: tracepanel's
OpenTelemetry integration leaves those out (tracepanel.integrations.otel), so a call shows once.
"""

import importlib
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from tracepanel.core.imports import when_imported
from tracepanel.core.model import Attrs
from tracepanel.integrations import _llm
from tracepanel.integrations._llm import Endpoint, Provider, Reply, Usage, get, items, settings, text, tool_call

_SETTINGS = {
    "max_tokens": "max_tokens",
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "stop_sequences": "stop_sequences",
}
_TOOL_USES = frozenset({"tool_use", "server_tool_use", "mcp_tool_use"})


def _name(client: object) -> str:
    name = type(client).__name__
    if "Bedrock" in name:
        return "aws.bedrock"
    if "Vertex" in name:
        return "gcp.vertex_ai"
    return "anthropic"


def _request(body: Mapping[str, Any]) -> Attrs:
    model = body.get("model")
    attrs = {**({"gen_ai.request.model": model} if isinstance(model, str) else {}), **settings(body, _SETTINGS)}
    effort = get(body.get("output_config"), "effort")
    if isinstance(effort, str):
        attrs["gen_ai.request.reasoning.level"] = effort
    return attrs


def _block(block: object) -> dict[str, object]:
    """A content block as the conventions' part: text, a tool's call or its result, thinking; else its type."""
    kind = get(block, "type")
    if kind == "text":
        return text(get(block, "text"))
    if kind in _TOOL_USES:
        return tool_call(get(block, "id"), get(block, "name"), get(block, "input"))
    if kind == "tool_result":
        return {"type": "tool_call_response", "id": get(block, "tool_use_id"), "response": get(block, "content")}
    if kind == "thinking":
        return {"type": "reasoning", "content": get(block, "thinking")}
    return {"type": kind}


def _parts(content: object) -> list[object]:
    return [text(content)] if isinstance(content, str) else [_block(block) for block in items(content)]


def _messages(body: Mapping[str, Any]) -> Attrs:
    messages = [
        {"role": get(message, "role"), "parts": _parts(get(message, "content"))}
        for message in items(body.get("messages"))
    ]
    attrs = _llm.content("gen_ai.input.messages", messages)
    system = body.get("system")
    if system:
        attrs.update(_llm.content("gen_ai.system_instructions", _parts(system)))
    return attrs


def _usage(usage: object, into: Usage | None = None) -> Usage:
    """The usage a message or its stream's last event reports, over what an earlier event did."""
    found = into or Usage()
    if usage is None:
        return found
    read, written = get(usage, "cache_read_input_tokens"), get(usage, "cache_creation_input_tokens")
    found.cache_read = read if isinstance(read, int) else found.cache_read
    found.cache_write = written if isinstance(written, int) else found.cache_write
    given = get(usage, "input_tokens")
    if isinstance(given, int):
        found.input = given + (found.cache_read or 0) + (found.cache_write or 0)
    output = get(usage, "output_tokens")
    found.output = output if isinstance(output, int) else found.output
    return found


def _reply(result: object, capture: bool) -> Reply | None:  # noqa: FBT001 (the endpoint's callback)
    content = get(result, "content")
    if content is None:
        return None
    reason = get(result, "stop_reason")
    reply = Reply(
        id=get(result, "id"),
        model=get(result, "model"),
        finish_reasons=[str(reason) if reason is not None else "error"],
        usage=_usage(get(result, "usage")),
        tools=[str(get(block, "name")) for block in items(content) if get(block, "type") in _TOOL_USES],
    )
    if capture:
        reply.messages = [{"role": "assistant", "parts": _parts(content), "finish_reason": reason}]
    return reply


class _Chunks:
    """A streamed message: message_start's id, model and input usage, each block's content, message_delta's end."""

    def __init__(self, capture: bool) -> None:  # noqa: FBT001
        self.capture = capture
        self.reply_ = Reply()
        self.reason: str | None = None
        # Each content block by its index: its part, and the pieces of its text, thinking or tool input.
        self.blocks: dict[int, tuple[dict[str, object], list[str]]] = {}

    def add(self, chunk: Any) -> None:  # noqa: ANN401 (a RawMessageStreamEvent)
        kind = get(chunk, "type")
        if kind == "message_start":
            message = get(chunk, "message")
            self.reply_.id, self.reply_.model = get(message, "id"), get(message, "model")
            self.reply_.usage = _usage(get(message, "usage"), self.reply_.usage)
        elif kind == "content_block_start":
            block = get(chunk, "content_block")
            if get(block, "type") in _TOOL_USES:
                self.reply_.tools.append(str(get(block, "name")))
            if self.capture:
                self.blocks[get(chunk, "index") or 0] = (_block(block), [])
        elif kind == "content_block_delta" and self.capture:
            delta = get(chunk, "delta")
            known = self.blocks.get(get(chunk, "index") or 0)
            piece = get(delta, "text") or get(delta, "thinking") or get(delta, "partial_json")
            if known is not None and isinstance(piece, str):
                known[1].append(piece)
        elif kind == "message_delta":
            self.reason = get(get(chunk, "delta"), "stop_reason") or self.reason
            self.reply_.usage = _usage(get(chunk, "usage"), self.reply_.usage)

    def reply(self) -> Reply:
        reply = self.reply_
        reply.finish_reasons = [self.reason or "error"]
        if self.capture:
            parts: list[object] = []
            for index in sorted(self.blocks):
                part, pieces = self.blocks[index]
                if pieces:
                    whole = "".join(pieces)
                    part = (
                        {**part, "arguments": _llm.arguments(whole)}
                        if part["type"] == "tool_call"
                        else {**part, "content": whole}
                    )
                parts.append(part)
            reply.messages = [{"role": "assistant", "parts": parts, "finish_reason": self.reason}]
        return reply


PROVIDER = Provider(name=_name, endpoints={"POST /v1/messages": Endpoint("chat", _request, _reply, _Chunks, _messages)})


def _patch(module: ModuleType) -> None:
    del module
    client = importlib.import_module("anthropic._base_client")
    streaming = importlib.import_module("anthropic._streaming")
    _llm.wrap(PROVIDER, client.SyncAPIClient, [streaming.Stream], asynchronous=False)
    _llm.wrap(PROVIDER, client.AsyncAPIClient, [streaming.AsyncStream], asynchronous=True)
    _llm.wrap_close(streaming.Stream, asynchronous=False)
    _llm.wrap_close(streaming.AsyncStream, asynchronous=True)


def install() -> None:
    """Wrap Anthropic's clients once the app imports the SDK."""
    when_imported("anthropic", _patch)
