"""Spans that give a request its shape: the view call and every template render, with the context it got.

The context is read once it is bound to the template (Context.bind_template), after RequestContext ran its context
processors: their `request`, `user`, `perms` and `csrf_token` are in it. A template rendered inside another (an
include) gets a context bound already: it is read as the render starts, in a span of its own. A template another
extends renders in that one's span, with its context, read once. Not Template._render: Django's test runner replaces
it.
"""

import contextlib
import functools
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from inspect import iscoroutinefunction
from types import FrameType
from typing import TYPE_CHECKING, Any

from django.core.handlers.base import BaseHandler
from django.template.base import Node, Template
from django.template.context import Context, RequestContext
from django.template.loader_tags import BlockNode

from tracepanel.core import frames, values
from tracepanel.core.context import current_span, current_trace, start_span
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


def context_items(context: Any) -> dict[str, object]:  # noqa: ANN401 (Django's Context)
    """A Context's names as the template sees them: later layers win, each name where it was first set.

    Its first layer is Django's builtins (True, False, None), `block` the {% block %} a template renders in (an
    include inside one sees it) and `forloop` the state of a {% for %} it renders in: Django's own, not the view's.
    """
    items: dict[str, object] = {}
    for layer in getattr(context, "dicts", [])[1:]:
        if isinstance(layer, dict):
            items.update(layer)
    if isinstance(items.get("block"), BlockNode):
        del items["block"]
    loop = items.get("forloop")
    if isinstance(loop, dict) and "parentloop" in loop and "counter0" in loop:
        del items["forloop"]
    return items


def record_context(context: Any) -> None:  # noqa: ANN401 (Django's Context)
    """The context into the template's span, once: an extended template renders in its child's span."""
    span = current_span()
    if span is None or span.kind != "template" or "template.context.names" in span.attrs:
        return
    if not values.context_wanted(current_trace(), span.name):
        return
    # The page renders whatever its context holds.
    with contextlib.suppress(Exception):
        span.attrs.update(values.context_attrs(context_items(context)))


def _node_place(frame: FrameType) -> tuple[str | None, int] | None:
    """The template and line of the node a frame of Node.render_annotated renders: a {% for %}, a {{ variable }}."""
    node = frame.f_locals.get("self")
    lineno = getattr(getattr(node, "token", None), "lineno", None)
    if not isinstance(lineno, int):
        return None
    name = getattr(getattr(node, "origin", None), "template_name", None)
    return (str(name) if name else None), lineno


def install() -> None:
    # Every node renders through render_annotated(): a query made under one is that node's, at its line.
    frames.template_frames(Node.render_annotated.__code__, _node_place)

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
                # Inside another template's render (an include): bound already, its context complete.
                if getattr(context, "template", None) is not None:
                    record_context(context)
                return original_render(self, context)

        patch(Template, "render", render)

    for owner in (Context, RequestContext):
        if not patched(owner, "bind_template"):
            patch(owner, "bind_template", _bound(vars(owner)["bind_template"]))


def _bound(original: Callable[..., AbstractContextManager[object]]) -> Callable[..., AbstractContextManager[None]]:
    """bind_template() that reads the context once it is bound (RequestContext's processors ran)."""

    @contextlib.contextmanager
    def bind_template(self: Context, template: Template) -> Iterator[None]:
        with original(self, template):
            record_context(self)
            yield

    return bind_template
