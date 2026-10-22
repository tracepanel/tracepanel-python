"""What every GraphQL server's trace says about its operation: Strawberry's and Graphene's look the same.

The operation's span and the request are named after it (`query PostList`, `query { posts, users }` when unnamed),
its document is kept, whitespace collapsed, and the exceptions resolvers raised are the trace's errors: the client
got them as `errors` with a 200. A document that didn't parse is no operation: `syntax error`, with no type or name,
so it doesn't group with the queries. One that parsed and failed validation keeps its operation's name.
`graphql.library` names the server (`strawberry`, `graphene`), so the agent's hints name its DataLoader.
"""

import re
from typing import TYPE_CHECKING, Any

from tracepanel.core import errors

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tracepanel.core.model import Attrs, Span, Trace

MAX_DOCUMENT = 2000
MAX_ROOT_FIELDS = 3
# What a document that didn't parse is named: it has no operation, so it groups apart from the anonymous queries.
SYNTAX_ERROR = "syntax error"
_WHITESPACE = re.compile(r"\s+")


def document_text(query: str | None) -> str | None:
    if not query:
        return None
    text = _WHITESPACE.sub(" ", query).strip()
    return text if len(text) <= MAX_DOCUMENT else f"{text[:MAX_DOCUMENT]}…"


def root_fields(document: Any) -> str | None:  # noqa: ANN401 (graphql-core's DocumentNode, or None)
    """`{ posts, users }`: what an unnamed operation asks for, so `{ posts { id } }` isn't only "anonymous"."""
    try:
        operations = [
            definition
            for definition in document.definitions
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


def finish_operation(  # noqa: PLR0913 (what an operation is)
    trace: "Trace",
    span: "Span | None",
    *,
    library: str,
    kind: str | None,
    name: str | None,
    query: str | None,
    document: Any,  # noqa: ANN401 (graphql-core's DocumentNode, or None when it didn't parse)
    failures: "Iterable[Any]",
) -> None:
    """Name the operation's span and the request after it, and record resolver exceptions as errors."""
    failures = list(failures)
    if document is None:
        # Not an operation of any type: the operationName the client sent names none either.
        kind = name = None
        label = SYNTAX_ERROR
    else:
        label = f"{kind} {name or root_fields(document) or 'anonymous'}"
    attrs: Attrs = {
        "graphql.operation.type": kind,
        "graphql.operation.name": name,
        "graphql.document": document_text(query),
        "graphql.errors": len(failures),
        "graphql.library": library,
    }
    if span is not None:
        span.name = label
        span.attrs.update(attrs)
    # The request is this operation: named and grouped after it.
    trace.attrs.update({**attrs, "graphql.operation": label})
    for failure in failures:
        # A GraphQLError wraps what a resolver raised; a syntax or validation error wraps nothing.
        original = getattr(failure, "original_error", failure)
        if isinstance(original, Exception):
            errors.capture(original, handled=False)
