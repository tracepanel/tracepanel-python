"""Spans that give a request its shape: the view call and every template render."""

import functools
from collections.abc import Awaitable, Callable
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

from django.core.handlers.base import BaseHandler
from django.template.base import Template

from tracepanel.core.context import start_span
from tracepanel.core.patching import patch, patched

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs

MARKER = "_tracepanel"
View = Callable[..., Any]


def _view_name(view: View) -> str:
    target = getattr(view, "view_class", None) or getattr(view, "cls", None) or view
    module = getattr(target, "__module__", "")
    name = getattr(target, "__qualname__", None) or type(target).__qualname__
    return f"{module}.{name}" if module else name


def wrap_view(view: View) -> View:
    if getattr(view, MARKER, False):
        return view
    name = _view_name(view)
    attrs: Attrs = {"code.view": name}

    if iscoroutinefunction(view):

        @functools.wraps(view)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (any view signature)
            with start_span("view", name, attrs):
                result: Awaitable[Any] = view(*args, **kwargs)
                return await result

        setattr(async_wrapped, MARKER, True)
        return async_wrapped

    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (any view signature)
        with start_span("view", name, attrs):
            return view(*args, **kwargs)

    setattr(wrapped, MARKER, True)
    return wrapped


def _template_name(template: Template) -> str:
    origin = getattr(template, "origin", None)
    name = getattr(origin, "template_name", None) or getattr(template, "name", None)
    return str(name) if name else "<string template>"


def install() -> None:
    # Django calls make_view_atomic() on the resolved view right before calling it.
    if not patched(BaseHandler, "make_view_atomic"):
        original_atomic = BaseHandler.make_view_atomic

        @functools.wraps(original_atomic)
        def make_view_atomic(self: BaseHandler, view: View) -> View:
            return wrap_view(original_atomic(self, view))

        patch(BaseHandler, "make_view_atomic", make_view_atomic)

    if not patched(Template, "render"):
        original_render = Template.render

        @functools.wraps(original_render)
        def render(self: Template, context: Any) -> Any:  # noqa: ANN401 (Django's signature)
            name = _template_name(self)
            attrs: Attrs = {"template.name": name}
            with start_span("template", name, attrs):
                return original_render(self, context)

        patch(Template, "render", render)
