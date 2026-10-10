"""Strawberry GraphQL: operations, their phases and the project's own resolvers as spans.

Added to every schema on its own (no `extensions=[...]` needed). The request trace is named after the operation
(`GraphQL query PostList`), so traffic groups by operation instead of `POST /graphql/`. Resolvers that only read an
attribute are left out: a list of 500 objects would otherwise add thousands of spans. Exceptions raised in
resolvers come back to the client as `errors` with a 200; they are recorded as the trace's errors.
"""

import inspect
import re
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Any

from tracepanel._compat import override
from tracepanel.core import errors
from tracepanel.core.context import current_trace, start_span
from tracepanel.core.patching import patch, patched

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs, Span, Trace

MAX_DOCUMENT = 2000
_WHITESPACE = re.compile(r"\s+")


def _document(query: str | None) -> str | None:
    if not query:
        return None
    text = _WHITESPACE.sub(" ", query).strip()
    return text if len(text) <= MAX_DOCUMENT else f"{text[:MAX_DOCUMENT]}…"


def _operation(context: Any) -> tuple[str, str | None]:  # noqa: ANN401 (Strawberry's ExecutionContext)
    """(type, name) of the operation; the type is unknown until the document parses."""
    try:
        kind = str(context.operation_type.value)
    except Exception:  # noqa: BLE001 (no document: a syntax error, reported by Strawberry itself)
        kind = "query"
    try:
        name = context.operation_name
    except Exception:  # noqa: BLE001
        name = None
    return kind, name


MAX_ROOT_FIELDS = 3


def _root_fields(context: Any) -> str | None:  # noqa: ANN401 (Strawberry's ExecutionContext)
    """`{ posts, users }`: what an unnamed operation asks for, so `{ posts { id } }` isn't only "anonymous"."""
    try:
        operations = [
            definition
            for definition in context.graphql_document.definitions
            if type(definition).__name__ == "OperationDefinitionNode" and definition.name is None
        ]
        selections = operations[0].selection_set.selections
    except Exception:  # noqa: BLE001 (no parsed document: a syntax error)
        return None
    fields = [selection.name.value for selection in selections if type(selection).__name__ == "FieldNode"]
    if not fields:
        return None
    more = len(fields) - MAX_ROOT_FIELDS
    shown = ", ".join(fields[:MAX_ROOT_FIELDS]) + (f", +{more}" if more > 0 else "")
    return f"{{ {shown} }}"


def _custom_resolver(info: Any) -> Any:  # noqa: ANN401 (GraphQL's resolve info)
    """The project's resolver for this field, or None for fields that read an attribute."""
    field = info.parent_type.fields.get(info.field_name)
    definition = field.extensions.get("strawberry-definition") if field is not None and field.extensions else None
    return getattr(definition, "base_resolver", None)


def _path(info: Any) -> str:  # noqa: ANN401
    keys: list[str] = []
    path = info.path
    while path is not None:
        keys.append(str(path.key))
        path = path.prev
    return ".".join(reversed(keys))


def _finish_operation(context: Any, trace: "Trace", span: "Span | None") -> None:  # noqa: ANN401 (ExecutionContext)
    """Name the operation's span and the request after it, and record resolver exceptions as errors."""
    kind, name = _operation(context)
    label = f"{kind} {name or _root_fields(context) or 'anonymous'}"
    failures = list(getattr(context.result, "errors", None) or context.pre_execution_errors or [])
    attrs: Attrs = {
        "graphql.operation.type": kind,
        "graphql.operation.name": name,
        "graphql.document": _document(context.query),
        "graphql.errors": len(failures),
    }
    if span is not None:
        span.name = label
        span.attrs.update(attrs)
    # The request is this operation: named and grouped after it.
    trace.attrs.update({**attrs, "graphql.operation": label})
    for failure in failures:
        original = getattr(failure, "original_error", None)
        if isinstance(original, Exception):
            errors.capture(original, handled=False)


def _extension_class() -> type:
    from strawberry.extensions import SchemaExtension  # noqa: PLC0415 (optional dependency)

    class TracepanelExtension(SchemaExtension):
        @override
        def on_operation(self) -> Generator[None, None, None]:
            trace = current_trace()
            if trace is None:
                yield
                return
            with start_span("graphql", "graphql operation", {}) as span:
                yield
                _finish_operation(self.execution_context, trace, span)

        @override
        def on_parse(self) -> Generator[None, None, None]:
            with start_span("graphql", "parse", {}):
                yield

        @override
        def on_validate(self) -> Generator[None, None, None]:
            with start_span("graphql", "validate", {}):
                yield

        @override
        def resolve(
            self,
            _next: Callable[..., Any],
            root: Any,  # noqa: ANN401 (any parent object)
            info: Any,  # noqa: ANN401
            *args: Any,  # noqa: ANN401
            **kwargs: Any,  # noqa: ANN401
        ) -> Any:  # noqa: ANN401
            resolver = _custom_resolver(info) if current_trace() is not None else None
            if resolver is None:
                return _next(root, info, *args, **kwargs)
            name = f"{info.parent_type.name}.{info.field_name}"
            attrs: Attrs = {"graphql.field": name, "graphql.path": _path(info), "code.function": name}
            if getattr(resolver, "is_async", False):

                async def run() -> Any:  # noqa: ANN401
                    with start_span("function", name, attrs):
                        result = _next(root, info, *args, **kwargs)
                        return await result if inspect.isawaitable(result) else result

                return run()
            with start_span("function", name, attrs):
                return _next(root, info, *args, **kwargs)

    return TracepanelExtension


def install() -> None:
    """Add tracepanel to every Strawberry schema when Strawberry is installed; a no-op otherwise."""
    try:
        from strawberry.schema.schema import Schema  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    if patched(Schema, "get_extensions"):
        return
    extension = _extension_class()
    original = Schema.get_extensions

    def get_extensions(self: Schema, sync: bool = False) -> list[Any]:  # noqa: FBT001, FBT002 (Strawberry's)
        return [*original(self, sync=sync), extension()]

    patch(Schema, "get_extensions", get_extensions)
