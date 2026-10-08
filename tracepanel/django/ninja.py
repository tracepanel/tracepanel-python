"""Django Ninja: the operation a request ran, and the time Ninja itself spent around it.

Ninja registers one Django view per path, shared by every method on it: without this, `GET /api/orders` and
`POST /api/orders` both show Ninja's own wrapper as their view. The view span is renamed after the project's
function (`shop.api.list_orders`), and inside it Ninja's steps become `framework` spans:

- `auth` — the auth callbacks; a refusal (401) says which ones refused;
- `validate` — parsing path, query and body into the operation's schemas; a 422 lists what was wrong;
- `response` — turning the result into JSON through the response schema, where lazy relations run their queries.
"""

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tracepanel.core.context import current_span, current_trace, start_span
from tracepanel.core.patching import patch, patched
from tracepanel.django.signals import qualname

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs

MAX_ERRORS = 20


def _name_view(operation: Any) -> None:  # noqa: ANN401 (ninja.operation.Operation)
    """Name the view span and the trace's view after the operation's own function."""
    trace = current_trace()
    if trace is None:
        return
    view = qualname(operation.view_func)
    trace.attrs["code.view"] = view
    try:
        # What the OpenAPI schema calls it: the explicit operation_id, else Ninja's default (shop_api_list_orders).
        operation_id = operation.operation_id or operation.api.get_openapi_operation_id(operation)
    except Exception:  # noqa: BLE001 (a custom get_openapi_operation_id must not break the request)
        operation_id = None
    if operation_id:
        trace.attrs["ninja.operation_id"] = operation_id
    span = current_span()
    if span is not None and span.kind == "view":
        span.name = view
        span.attrs["code.view"] = view


def _auth_names(operation: Any) -> list[str | int | float | bool | None]:  # noqa: ANN401
    return [qualname(callback) for callback in operation.auth_callbacks]


def _validation_errors(exc: BaseException) -> list[str | int | float | bool | None]:
    """`query.limit: Input should be a valid integer` per problem, as the client got them in the 422."""
    lines: list[str | int | float | bool | None] = []
    for error in getattr(exc, "errors", [])[:MAX_ERRORS]:
        where = ".".join(str(part) for part in error.get("loc", ()))
        lines.append(f"{where}: {error.get('msg', '')}" if where else str(error.get("msg", "")))
    return lines


def _wrap_get_values(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def get_values(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(self, *args, **kwargs)
        with start_span("framework", "ninja validate", {"framework.step": "validate"}) as span:
            try:
                return original(self, *args, **kwargs)
            except Exception as exc:
                if span is not None:
                    span.attrs["validation.errors"] = _validation_errors(exc)
                raise

    return get_values


def _wrap_result(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def result_to_response(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(self, *args, **kwargs)
        with start_span("framework", "ninja response", {"framework.step": "response"}):
            return original(self, *args, **kwargs)

    return result_to_response


def _auth_attrs(operation: Any) -> "Attrs":  # noqa: ANN401
    return {"framework.step": "auth", "auth.callbacks": _auth_names(operation)}


def _wrap_auth(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def run_authentication(self: Any, request: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(self, request)
        with start_span("framework", "ninja auth", _auth_attrs(self)) as span:
            refused = original(self, request)
            if span is not None:
                span.attrs["auth.result"] = "refused" if refused is not None else "ok"
            return refused

    return run_authentication


def _wrap_async_auth(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def run_authentication(self: Any, request: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return await original(self, request)
        with start_span("framework", "ninja auth", _auth_attrs(self)) as span:
            refused = await original(self, request)
            if span is not None:
                span.attrs["auth.result"] = "refused" if refused is not None else "ok"
            return refused

    return run_authentication


def _wrap_run(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def run(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        _name_view(self)
        return original(self, *args, **kwargs)

    return run


def _wrap_async_run(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def run(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        _name_view(self)
        return await original(self, *args, **kwargs)

    return run


def install() -> None:
    """Patch Ninja's operations when Django Ninja is installed; a no-op otherwise."""
    try:
        from ninja.operation import AsyncOperation, Operation  # noqa: PLC0415 (optional dependency)
    except ImportError:
        return
    if patched(Operation, "run"):
        return
    patches = [
        (Operation, "run", _wrap_run),
        (AsyncOperation, "run", _wrap_async_run),
        (Operation, "_run_authentication", _wrap_auth),
        (AsyncOperation, "_run_authentication", _wrap_async_auth),
        # Shared by both: AsyncOperation inherits them.
        (Operation, "_get_values", _wrap_get_values),
        (Operation, "_result_to_response", _wrap_result),
    ]
    for cls, name, wrap in patches:
        patch(cls, name, wrap(cls.__dict__[name]))
