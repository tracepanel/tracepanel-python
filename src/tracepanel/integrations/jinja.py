"""Jinja: each render of a template as a `template` span, wherever the app renders it.

Starlette's and FastAPI's Jinja2Templates, Flask's render_template, Django's Jinja2 backend and a template rendered by
hand all end in Template.render (render_async under an async environment): one span each, named after the template's
file, or `<string template>` for one made from a string. A query run inside it says which template it came from
(code.template).

Templates a template includes or extends render inside its render, in its span. A template streamed with generate()
renders as the response goes out, after the view: not a span.
"""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from tracepanel.core.context import start_span
from tracepanel.core.imports import when_imported
from tracepanel.core.patching import patch, patched

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

STRING = "<string template>"
# Set while render() runs: under an async environment it runs render_async, which is then the same render, not another.
_rendering: ContextVar[bool] = ContextVar("tracepanel_jinja_rendering", default=False)


def _name(template: Any) -> str:  # noqa: ANN401 (a Jinja Template)
    return str(template.name) if template.name else STRING


def _wrap(module: Any) -> None:  # noqa: ANN401 (jinja2.environment)
    template = module.Template
    if patched(template, "render"):
        return
    render: Callable[..., str] = template.render
    render_async: Callable[..., Coroutine[Any, Any, str]] = template.render_async

    def traced(self: Any, *args: Any, **kwargs: Any) -> str:  # noqa: ANN401
        name = _name(self)
        token = _rendering.set(True)
        try:
            with start_span("template", name, {"template.name": name}):
                return render(self, *args, **kwargs)
        finally:
            _rendering.reset(token)

    async def traced_async(self: Any, *args: Any, **kwargs: Any) -> str:  # noqa: ANN401
        if _rendering.get():
            return await render_async(self, *args, **kwargs)
        name = _name(self)
        with start_span("template", name, {"template.name": name}):
            return await render_async(self, *args, **kwargs)

    patch(template, "render", traced)
    patch(template, "render_async", traced_async)


def install() -> None:
    """Wrap Jinja's Template as the app imports it."""
    when_imported("jinja2.environment", _wrap)
