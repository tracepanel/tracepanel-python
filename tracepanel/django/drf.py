"""Django REST framework: the action a request ran, and DRF's own steps around it.

The view span is named after what handled the request: the ViewSet action (`blog.views.PostViewSet.list`), the
method of an APIView (`blog.views.PostFixedList.get`) or the `@api_view` function (`blog.views.me`), so the actions
of one ViewSet don't merge into one endpoint. DRF's steps become `framework` spans inside it:

- `drf auth` — the authentication classes, and whether any of them recognised the request;
- `drf permissions` — the permission classes; a 401/403 names the one that refused, with its message;
- `drf throttles` — only for views with throttles; a 429 says how long to wait;
- `drf validate <Serializer>` — `is_valid()`; a 400 lists what was wrong (`body: This field is required.`);
- `drf serialize <Serializer>` — `.data`, where nested serializers and SerializerMethodFields run their queries.
"""

import functools
import sys
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from tracepanel.core.context import current_span, current_trace, start_span
from tracepanel.core.patching import patch, patched
from tracepanel.django.signals import qualname

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs, Scalar

MAX_ERRORS = 20
# @api_view builds a class with type("WrappedAPIView", ...), then gives it the function's name and module.
API_VIEW = "WrappedAPIView"


def _handler(view: Any, request: Any) -> str:  # noqa: ANN401 (rest_framework.views.APIView)
    cls = type(view)
    name = f"{cls.__module__}.{cls.__name__}"
    if cls.__qualname__ == API_VIEW:
        return name
    action = getattr(view, "action", None) or str(request.method).lower()
    return f"{cls.__module__}.{cls.__qualname__}.{action}"


def _name_view(view: Any, request: Any) -> None:  # noqa: ANN401
    trace = current_trace()
    if trace is None:
        return
    name = _handler(view, request)
    trace.attrs["code.view"] = name
    span = current_span()
    if span is not None and span.kind == "view":
        span.name = name
        span.attrs["code.view"] = name


def _names(objects: Any) -> "list[Scalar]":  # noqa: ANN401
    return [qualname(type(obj)) for obj in objects]


def _flatten(detail: Any, path: str = "") -> Iterator[str]:  # noqa: ANN401 (DRF's ReturnDict / ErrorDetail tree)
    """`post: Invalid pk "9" - object does not exist.` per problem, nested fields joined with dots."""
    if isinstance(detail, dict):
        for key, value in detail.items():
            field = "" if key == "non_field_errors" else str(key)
            yield from _flatten(value, f"{path}.{field}" if path and field else path or field)
    elif isinstance(detail, list):
        for index, value in enumerate(detail):
            nested = isinstance(value, (dict, list))
            yield from _flatten(value, f"{path}.{index}" if nested and path else str(index) if nested else path)
    else:
        yield f"{path}: {detail}" if path else str(detail)


def _serializer_name(serializer: Any) -> str:  # noqa: ANN401 (rest_framework.serializers.BaseSerializer)
    child = getattr(serializer, "child", None)
    return f"{type(child).__name__}(many=True)" if child is not None else type(serializer).__name__


def _wrap_initial(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def initial(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        _name_view(self, request)
        return original(self, request, *args, **kwargs)

    return initial


def _wrap_authentication(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def perform_authentication(self: Any, request: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(self, request)
        attrs: Attrs = {"framework.step": "auth", "auth.classes": _names(request.authenticators)}
        with start_span("framework", "drf auth", attrs) as span:
            result = original(self, request)
            if span is not None:
                authenticator = request.successful_authenticator
                span.attrs["auth.result"] = qualname(type(authenticator)) if authenticator else "anonymous"
            return result

    return perform_authentication


def _wrap_permissions(original: Callable[..., Any], name: str) -> Callable[..., Any]:
    @functools.wraps(original)
    def check(self: Any, request: Any, *args: Any) -> Any:  # noqa: ANN401
        if current_trace() is None:
            return original(self, request, *args)
        attrs: Attrs = {"framework.step": "permissions", "permission.classes": _names(self.get_permissions())}
        with start_span("framework", name, attrs):
            return original(self, request, *args)

    return check


def _wrap_permission_denied(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def permission_denied(self: Any, request: Any, message: Any = None, code: Any = None) -> Any:  # noqa: ANN401
        span = current_span()
        if span is not None and span.attrs.get("framework.step") == "permissions":
            # Called from check_permissions' loop: the permission that refused is its loop variable.
            permission = sys._getframe(1).f_locals.get("permission")  # noqa: SLF001
            if permission is not None:
                span.attrs["permission.refused"] = qualname(type(permission))
        try:
            return original(self, request, message=message, code=code)
        except Exception as exc:
            if span is not None and span.attrs.get("framework.step") == "permissions":
                # What the client got: the permission's message, or DRF's NotAuthenticated text without one.
                span.attrs["permission.message"] = str(getattr(exc, "detail", exc))
            raise

    return permission_denied


def _wrap_throttles(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def check_throttles(self: Any, request: Any) -> Any:  # noqa: ANN401
        throttles = self.get_throttles()
        if current_trace() is None or not throttles:
            return original(self, request)
        attrs: Attrs = {"framework.step": "throttles", "throttle.classes": _names(throttles)}
        with start_span("framework", "drf throttles", attrs):
            return original(self, request)

    return check_throttles


def _wrap_throttled(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def throttled(self: Any, request: Any, wait: Any) -> Any:  # noqa: ANN401
        span = current_span()
        if span is not None and span.attrs.get("framework.step") == "throttles":
            span.attrs["throttle.wait"] = None if wait is None else round(float(wait), 1)
        return original(self, request, wait)

    return throttled


def _wrap_is_valid(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def is_valid(self: Any, *, raise_exception: bool = False) -> bool:  # noqa: ANN401
        if current_trace() is None or hasattr(self, "_validated_data"):
            return original(self, raise_exception=raise_exception)
        name = _serializer_name(self)
        attrs: Attrs = {"framework.step": "validate", "serializer.class": qualname(type(getattr(self, "child", self)))}
        with start_span("framework", f"drf validate {name}", attrs) as span:
            try:
                return original(self, raise_exception=raise_exception)
            finally:
                errors = getattr(self, "_errors", None)
                if span is not None and errors:
                    span.status = "error"
                    problems: list[Scalar] = []
                    problems.extend(list(_flatten(errors))[:MAX_ERRORS])
                    span.attrs["validation.errors"] = problems

    return is_valid


def _wrap_data(original: property) -> property:
    getter: Callable[[Any], Any] = original.fget  # type: ignore[assignment]  # a property DRF defines

    @functools.wraps(getter)
    def data(self: Any) -> Any:  # noqa: ANN401
        # Only the outermost serializer, once: nested ones run inside its to_representation(), and .data is cached.
        if current_trace() is None or hasattr(self, "_data") or getattr(self, "parent", None) is not None:
            return getter(self)
        name = _serializer_name(self)
        attrs: Attrs = {"framework.step": "serialize", "serializer.class": qualname(type(getattr(self, "child", self)))}
        with start_span("framework", f"drf serialize {name}", attrs) as span:
            result = getter(self)
            if span is not None and isinstance(result, list):
                span.attrs["serializer.items"] = len(result)
            return result

    return property(data, original.fset, original.fdel, original.__doc__)


def install() -> None:
    """Patch DRF's views and serializers when Django REST framework is installed; a no-op otherwise."""
    try:
        from rest_framework.serializers import BaseSerializer, ListSerializer  # noqa: PLC0415 (optional dependency)
        from rest_framework.views import APIView  # noqa: PLC0415
    except ImportError:
        return
    if patched(APIView, "initial"):
        return
    patches: list[tuple[type, str, Callable[..., Any]]] = [
        (APIView, "initial", _wrap_initial(APIView.initial)),
        (APIView, "perform_authentication", _wrap_authentication(APIView.perform_authentication)),
        (APIView, "check_permissions", _wrap_permissions(APIView.check_permissions, "drf permissions")),
        (
            APIView,
            "check_object_permissions",
            _wrap_permissions(APIView.check_object_permissions, "drf object permissions"),
        ),
        (APIView, "permission_denied", _wrap_permission_denied(APIView.permission_denied)),
        (APIView, "check_throttles", _wrap_throttles(APIView.check_throttles)),
        (APIView, "throttled", _wrap_throttled(APIView.throttled)),
        (BaseSerializer, "is_valid", _wrap_is_valid(BaseSerializer.is_valid)),
        (ListSerializer, "is_valid", _wrap_is_valid(ListSerializer.is_valid)),
    ]
    for cls, name, replacement in patches:
        patch(cls, name, replacement)
    # Serializer and ListSerializer override .data with super().data: patching the base covers both.
    patch(BaseSerializer, "data", _wrap_data(BaseSerializer.__dict__["data"]))
