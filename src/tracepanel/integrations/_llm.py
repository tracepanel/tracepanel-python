"""LLM calls: each call an LLM provider's SDK makes to its API as a span, by OpenTelemetry's GenAI conventions.

tracepanel.integrations.openai and tracepanel.integrations.anthropic wrap their SDK's client where every API call goes
(its request(), which retries): a call to an endpoint they know is a span, `chat gpt-5` or `chat claude-sonnet-5-5`,
under the span current where the app made it. Its HTTP calls (one per attempt) are its children. Its attributes are
the conventions' `gen_ai.*`: the provider, the operation, the model asked for and the one that answered, the request's
settings (max_tokens, temperature), why the model stopped, the tokens it used (cost is the agent's to work out), and the
names of the tools it called. A failed call (a rate limit, an overloaded API, a timeout) is an error, `error.type` its
exception's class, with the HTTP status the API answered.

A stream's span lasts until the app has read it to the end, closed it or dropped it: its first chunk's time is
`gen_ai.response.time_to_first_chunk` (seconds, as the conventions have it), and the usage the last chunks carry is
counted. The span is current only while the call is made: what the app does while it reads the stream is not inside.
The conventions have no attribute for the tools a model called: `gen_ai.response.tool_calls` names them, in order, and
`gen_ai.response.tool_call_count` counts them.

The messages sent and received are kept only with `capture_llm_content` (off by default: they are the app's users'
words), in the conventions' shape (`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`),
masked as a request's JSON body is and cut at `max_body_kb`.

The spans are of their own kind, `llm`, their attributes under `gen_ai.`.
"""

import json
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from tracepanel.core import conf
from tracepanel.core import request as details
from tracepanel.core.context import OpenSpan, current_trace, open_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, AttrValue, Scalar
from tracepanel.core.patching import patch

KIND = "llm"
# Where a stream's call waits for its end: on the SDK's stream object.
MARKER = "_tracepanel_llm"


def get(value: object, name: str) -> Any:  # noqa: ANN401 (whatever the SDK's model or the JSON holds)
    """A field of an SDK's model (pydantic) or of a JSON object, None when it has none."""
    if isinstance(value, Mapping):
        return value.get(name)  # type: ignore[union-attr]
    return getattr(value, name, None)


def items(value: object) -> list[Any]:
    """A list field's items; nothing for anything else."""
    return list(value) if isinstance(value, (list, tuple)) else []  # type: ignore[arg-type]


def _scalar(value: object) -> Scalar:
    return value if isinstance(value, (str, int, float, bool)) else None


def settings(body: Mapping[str, Any], names: Mapping[str, str]) -> Attrs:
    """`gen_ai.request.<attr>` for each request field in `names` (field -> attr) the body has."""
    attrs: Attrs = {}
    for name, attr in names.items():
        value = body.get(name)
        if isinstance(value, str) and attr == "stop_sequences":
            attrs[f"gen_ai.request.{attr}"] = [value]
        elif isinstance(value, (list, tuple)):
            attrs[f"gen_ai.request.{attr}"] = [str(item) for item in value]  # type: ignore[misc]
        elif _scalar(value) is not None:
            attrs[f"gen_ai.request.{attr}"] = _scalar(value)
    return attrs


def arguments(raw: object) -> object:
    """A tool call's arguments as JSON, so their secrets are masked: the text when it isn't JSON (a stream's part)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def text(content: object) -> dict[str, object]:
    return {"type": "text", "content": content}


def tool_call(call_id: object, name: object, args: object) -> dict[str, object]:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments(args)}


@dataclass(slots=True)
class Usage:
    """Tokens, as the conventions count them: the input's total includes what the cache served or stored."""

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    reasoning: int | None = None

    def attrs(self) -> Attrs:
        attrs: Attrs = {}
        for value, attr in (
            (self.input, "input_tokens"),
            (self.output, "output_tokens"),
            (self.cache_read, "cache_read.input_tokens"),
            (self.cache_write, "cache_write.input_tokens"),
            (self.reasoning, "reasoning.output_tokens"),
        ):
            # The detailed counts only when there are some, as the conventions ask.
            if isinstance(value, int) and (value or attr in {"input_tokens", "output_tokens"}):
                attrs[f"gen_ai.usage.{attr}"] = value
        return attrs


@dataclass(slots=True)
class Reply:
    """What a call's answer says: from the response, or put together from a stream's chunks."""

    id: str | None = None
    model: str | None = None
    finish_reasons: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    tools: list[str] = field(default_factory=list)
    # The output messages in the conventions' shape: only with capture_llm_content.
    messages: list[object] | None = None

    def attrs(self) -> Attrs:
        attrs: Attrs = {}
        if self.id:
            attrs["gen_ai.response.id"] = self.id
        if self.model:
            attrs["gen_ai.response.model"] = self.model
        if self.finish_reasons:
            attrs["gen_ai.response.finish_reasons"] = list[Scalar](self.finish_reasons)
        attrs.update(self.usage.attrs())
        if self.tools:
            attrs["gen_ai.response.tool_calls"] = list[Scalar](self.tools)
            attrs["gen_ai.response.tool_call_count"] = len(self.tools)
        if self.messages is not None:
            attrs.update(content("gen_ai.output.messages", self.messages))
        return attrs


class Chunks(Protocol):
    """A stream's chunks, read as they come: what the call's answer was, once the stream is over."""

    def add(self, chunk: Any) -> None: ...  # noqa: ANN401 (an SDK's chunk or event)

    def reply(self) -> Reply: ...


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One endpoint a provider knows: the operation it is, and how to read its request and its answer."""

    operation: str
    # The request's attributes from its JSON body (the model, its settings).
    request: Callable[[Mapping[str, Any]], Attrs]
    # The answer, from the SDK's response object: None when it isn't one (a raw response the app parses itself).
    reply: Callable[[Any, bool], Reply | None]
    # What reads a stream's chunks (with the content, or not).
    chunks: Callable[[bool], Chunks] | None = None
    # The messages sent and the system instructions, in the conventions' shape (capture_llm_content).
    messages: Callable[[Mapping[str, Any]], Attrs] | None = None


@dataclass(frozen=True, slots=True)
class Provider:
    # gen_ai.provider.name for a client, by its class.
    name: Callable[[object], str]
    # The endpoints, by method and path ("POST /chat/completions").
    endpoints: Mapping[str, Endpoint]
    # Attributes of the provider's own (openai.api.type), by path.
    extra: Mapping[str, Attrs] = field(default_factory=dict)


def capturing() -> bool:
    return conf.get_config().capture_llm_content


def content(attr: str, value: object) -> Attrs:
    """A message list as JSON text, masked as a request's body is, cut at max_body_kb."""
    limit = max(conf.get_config().max_body_kb, 0) * 1024
    if not limit:
        return {}
    shown = details.json_text(value)
    if len(shown) <= limit:
        return {attr: shown}
    return {attr: shown[:limit], f"{attr}.truncated": True}


def _server(client: object) -> Attrs:
    parts = urlsplit(str(getattr(client, "base_url", "") or ""))
    if not parts.hostname:
        return {}
    return {"server.address": parts.hostname, "server.port": parts.port or (443 if parts.scheme == "https" else 80)}


class Call:
    """One API call's span: open while the SDK makes the call, then until a stream it returned is over."""

    def __init__(self, opened: OpenSpan, endpoint: Endpoint, *, capture: bool) -> None:
        self.opened = opened
        self.endpoint = endpoint
        self.capture = capture
        self.chunks: Chunks | None = None
        self.first = False
        self.done = False

    @property
    def attrs(self) -> dict[str, AttrValue]:
        return self.opened.span.attrs

    def failed(self, exc: BaseException) -> None:
        """The call raised: its span is an error, ended now."""
        self._error(exc)
        self.opened.end()
        self.done = True

    def _error(self, exc: BaseException) -> None:
        self.opened.span.status = "error"
        self.attrs["error.type"] = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            self.attrs["http.response.status_code"] = status

    def returned(self, result: object, *, stream: bool) -> None:
        """The call returned: its answer read now, or its stream followed until it ends."""
        self.opened.end()
        if stream and self.endpoint.chunks is not None:
            self.chunks = self.endpoint.chunks(self.capture)
            return
        self.done = True
        reply = self.endpoint.reply(result, self.capture)
        if reply is not None:
            self.attrs.update(reply.attrs())

    def chunk(self, chunk: object) -> None:
        if not self.first:
            self.first = True
            elapsed = (time.perf_counter_ns() - self.opened.started) / 1e9
            self.attrs["gen_ai.response.time_to_first_chunk"] = round(elapsed, 6)
        if self.chunks is not None:
            self.chunks.add(chunk)

    def finish(self, exc: BaseException | None = None) -> None:
        """The stream is over: read to its end, closed, dropped or broken by `exc`. Once."""
        if self.done:
            return
        self.done = True
        self.opened.span.duration = (time.perf_counter_ns() - self.opened.started) // 1000
        if self.chunks is not None:
            self.attrs.update(self.chunks.reply().attrs())
        if exc is not None:
            self._error(exc)


def begin(provider: Provider, client: object, options: object, *, stream: bool) -> Call | None:
    """The call's span, open and current, when the app makes a call to a known endpoint inside a trace."""
    if current_trace() is None:
        return None
    path = str(get(options, "url") or "").split("?", 1)[0]
    method = str(get(options, "method") or "").upper()
    endpoint = provider.endpoints.get(f"{method} {path}")
    if endpoint is None:
        return None
    body = get(options, "json_data")
    body = body if isinstance(body, Mapping) else {}
    capture = capturing()
    request = endpoint.request(body)  # type: ignore[arg-type]
    model = request.get("gen_ai.request.model")
    attrs: Attrs = {
        "gen_ai.provider.name": provider.name(client),
        "gen_ai.operation.name": endpoint.operation,
        **provider.extra.get(path, {}),
        **request,
        **({"gen_ai.request.stream": True} if stream else {}),
        **_server(client),
        **(endpoint.messages(body) if capture and endpoint.messages is not None else {}),  # type: ignore[arg-type]
        "code.origin": origin(),
    }
    name = f"{endpoint.operation} {model}" if model else endpoint.operation
    opened = open_span(KIND, name, attrs)
    return None if opened is None else Call(opened, endpoint, capture=capture)


def _observed(call: Call, iterator: Iterator[Any]) -> Iterator[Any]:
    failed: BaseException | None = None
    try:
        for chunk in iterator:
            call.chunk(chunk)
            yield chunk
    except GeneratorExit:
        raise
    except BaseException as exc:
        failed = exc
        raise
    finally:
        call.finish(failed)


async def _observed_async(call: Call, iterator: AsyncIterator[Any]) -> AsyncIterator[Any]:
    failed: BaseException | None = None
    try:
        async for chunk in iterator:
            call.chunk(chunk)
            yield chunk
    except GeneratorExit:
        raise
    except BaseException as exc:
        failed = exc
        raise
    finally:
        call.finish(failed)


def _follow(call: Call, result: object, streams: Sequence[type], *, asynchronous: bool) -> None:
    """Read a stream's chunks as the app does, through the SDK's own iterator, until it ends."""
    if not isinstance(result, tuple(streams)):
        call.finish()
        return
    iterator = result._iterator  # type: ignore[attr-defined]  # noqa: SLF001 (what the stream's iteration reads)
    result._iterator = _observed_async(call, iterator) if asynchronous else _observed(call, iterator)  # type: ignore[attr-defined]  # noqa: SLF001
    vars(result)[MARKER] = call


def wrap(provider: Provider, client_class: type, streams: Sequence[type], *, asynchronous: bool) -> None:
    """Wrap the SDK's client's request(): every API call, with its retries, as one span."""
    request = client_class.request  # type: ignore[attr-defined]

    if asynchronous:

        async def traced_async(self: Any, cast_to: Any, options: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (the SDK's signature)
            stream = bool(kwargs.get("stream"))
            call = begin(provider, self, options, stream=stream)
            if call is None:
                return await request(self, cast_to, options, *args, **kwargs)
            try:
                result = await request(self, cast_to, options, *args, **kwargs)
            except BaseException as exc:
                call.failed(exc)
                raise
            call.returned(result, stream=stream)
            if stream:
                _follow(call, result, streams, asynchronous=True)
            return result

        patch(client_class, "request", traced_async)
        return

    def traced(self: Any, cast_to: Any, options: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        stream = bool(kwargs.get("stream"))
        call = begin(provider, self, options, stream=stream)
        if call is None:
            return request(self, cast_to, options, *args, **kwargs)
        try:
            result = request(self, cast_to, options, *args, **kwargs)
        except BaseException as exc:
            call.failed(exc)
            raise
        call.returned(result, stream=stream)
        if stream:
            _follow(call, result, streams, asynchronous=False)
        return result

    patch(client_class, "request", traced)


def wrap_close(stream_class: type, *, asynchronous: bool) -> None:
    """A stream the app closes before its end ends its call's span there."""
    close = stream_class.close  # type: ignore[attr-defined]

    if asynchronous:

        async def closed_async(self: Any) -> None:  # noqa: ANN401
            try:
                await close(self)
            finally:
                call = vars(self).get(MARKER)
                if isinstance(call, Call):
                    call.finish()

        patch(stream_class, "close", closed_async)
        return

    def closed(self: Any) -> None:  # noqa: ANN401
        try:
            close(self)
        finally:
            call = vars(self).get(MARKER)
            if isinstance(call, Call):
                call.finish()

    patch(stream_class, "close", closed)
