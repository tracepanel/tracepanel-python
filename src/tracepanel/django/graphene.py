"""Graphene (graphene-django's GraphQLView): operations, their phases and the project's own resolvers as spans.

The trace looks like Strawberry's (tracepanel.integrations.strawberry), so the console shows both the same way: the
request is named after the operation (`GraphQL query PostList`), the operation's span holds `parse` and `validate`,
and each resolver of the project's is a `function` span. Every GraphQLView gets it on its own (no `MIDDLEWARE` in
GRAPHENE settings needed): the view's middleware gets one more, first, so it times the resolver alone. Fields that
only read an attribute are left out, as with Strawberry. Exceptions raised in resolvers come back to the client as
`errors` with a 200; they are recorded as the trace's errors. A batch view (`batch=True`) runs one operation span
per entry; the request is named after the last.
"""

import functools
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from tracepanel.core.context import current_trace, start_span
from tracepanel.core.patching import patch, patched
from tracepanel.integrations._graphql import finish_operation

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs

# The document the view's parse() returned, for the operation running now.
_parsed: ContextVar[Any] = ContextVar("tracepanel_graphene_document", default=None)


# Resolvers Graphene and graphene-django give a field that reads an attribute or a dict key, besides the default
# ones in graphene.types.resolver: a relay ID, a model's `id`.
_READERS = {("graphene.relay.node", "GlobalID.id_resolver"), ("graphene_django.types", "DjangoObjectType.resolve_id")}


def _attribute_read(info: Any) -> bool:  # noqa: ANN401 (GraphQL's resolve info)
    """Whether the field's resolver only reads an attribute: Graphene's default one, or a wrapper around it."""
    field = info.parent_type.fields.get(info.field_name)
    function = field.resolve if field is not None else None
    # graphene-django wraps the default resolver (choices, blank values) with functools.wraps, or in a partial.
    while True:
        if isinstance(function, functools.partial):
            function = function.func
        elif (inner := getattr(function, "__wrapped__", None)) is not None:
            function = inner
        else:
            break
    if function is None:
        return True
    module = getattr(function, "__module__", None)
    # The type's own default_resolver (Meta), when it sets one.
    meta = getattr(getattr(info.parent_type, "graphene_type", None), "_meta", None)
    return (
        module == "graphene.types.resolver"
        or (module, getattr(function, "__qualname__", None)) in _READERS
        or function is getattr(meta, "default_resolver", None)
    )


def _path(info: Any) -> str:  # noqa: ANN401
    keys: list[str] = []
    path = info.path
    while path is not None:
        keys.append(str(path.key))
        path = path.prev
    return ".".join(reversed(keys))


class TracepanelMiddleware:
    """Graphene middleware: a `function` span around each of the project's resolvers."""

    def resolve(
        self,
        next_: Callable[..., Any],
        root: Any,  # noqa: ANN401 (any parent object)
        info: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        if current_trace() is None or _attribute_read(info):
            return next_(root, info, **kwargs)
        name = f"{info.parent_type.name}.{info.field_name}"
        attrs: Attrs = {"graphql.field": name, "graphql.path": _path(info), "code.function": name}
        with start_span("function", name, attrs):
            return next_(root, info, **kwargs)


def _operation(document: Any, operation_name: str | None) -> tuple[str, str | None]:  # noqa: ANN401 (DocumentNode)
    """(type, name) of the operation; the type is unknown until the document parses."""
    from graphql import get_operation_ast  # noqa: PLC0415 (optional dependency)

    operation = get_operation_ast(document, operation_name) if document is not None else None
    if operation is None:
        return "query", operation_name
    return str(operation.operation.value), operation_name or (operation.name.value if operation.name else None)


def _wrap_execute(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def execute_graphql_request(  # noqa: PLR0913, PLR0917 (GraphQLView's)
        self: Any,  # noqa: ANN401
        request: Any,  # noqa: ANN401
        data: Any,  # noqa: ANN401
        query: str | None,
        variables: Any,  # noqa: ANN401
        operation_name: str | None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        trace = current_trace()
        if trace is None:
            return original(self, request, data, query, variables, operation_name, *args, **kwargs)
        token = _parsed.set(None)
        try:
            with start_span("graphql", "graphql operation", {}) as span:
                result = original(self, request, data, query, variables, operation_name, *args, **kwargs)
                # None: GraphiQL's page, no operation.
                if result is not None:
                    document = _parsed.get()
                    kind, name = _operation(document, operation_name)
                    failures = result.errors or []
                    finish_operation(
                        trace,
                        span,
                        library="graphene",
                        kind=kind,
                        name=name,
                        query=query,
                        document=document,
                        failures=failures,
                    )
                return result
        finally:
            _parsed.reset(token)

    return execute_graphql_request


def _wrap_parse(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def parse(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(*args, **kwargs)
        with start_span("graphql", "parse", {}):
            document = original(*args, **kwargs)
        _parsed.set(document)
        return document

    return parse


def _wrap_validate(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def validate(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        with start_span("graphql", "validate", {}):
            return original(*args, **kwargs)

    return validate


def _wrap_get_middleware(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def get_middleware(self: Any, request: Any) -> Any:  # noqa: ANN401
        middleware = original(self, request)
        if current_trace() is None:
            return middleware
        from graphql import MiddlewareManager  # noqa: PLC0415 (optional dependency)

        # First is innermost: the span times the resolver, not the project's own middleware around it.
        if isinstance(middleware, MiddlewareManager):
            return MiddlewareManager(TracepanelMiddleware(), *middleware.middlewares)
        return [TracepanelMiddleware(), *(middleware or ())]

    return get_middleware


def install() -> None:
    """Patch graphene-django's GraphQLView when graphene-django is installed; a no-op otherwise."""
    try:
        from graphene_django import views  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    view = views.GraphQLView
    if patched(view, "execute_graphql_request"):
        return
    patch(view, "execute_graphql_request", _wrap_execute(view.execute_graphql_request))
    patch(view, "get_middleware", _wrap_get_middleware(view.get_middleware))
    # The view calls the parse() and validate() it imported: those names are the phases.
    patch(views, "parse", _wrap_parse(views.parse))
    patch(views, "validate", _wrap_validate(views.validate))
