"""ASGI apps: Starlette and FastAPI in full, any other ASGI framework at the request level.

`tracepanel run uvicorn app:app` needs no code for Starlette and FastAPI: the app is set up as it is created. In code,
`app = tracepanel.asgi.install(app)` does the same; it is on only for an app in debug mode (`Starlette(debug=True)`) or
with TRACEPANEL_ENABLED=1. A Starlette or FastAPI app gets the middleware in place; any other ASGI app (Litestar, Quart,
a bare callable) comes back wrapped, so use what install() returns.

Requests become traces (status, errors, the widget's headers and script) and the console is served under
`/__tracepanel__/`. Starlette and FastAPI add the route's template and the endpoint as a `view` span; other frameworks'
requests are named after their path. The framework-free integrations turn on: SQLAlchemy, Strawberry, Celery,
logging. Settings are keyword arguments (the same names as Django's TRACEPANEL dict); TRACEPANEL_* variables override
them.
"""

from typing import Any

from tracepanel import _setup
from tracepanel.asgi.middleware import TracepanelMiddleware

MARKER = "_tracepanel"

__all__ = ["TracepanelMiddleware", "install"]


def install(app: Any, **settings: object) -> Any:  # noqa: ANN401 (any ASGI app)
    """Trace the app's requests; returns the app to serve (the same app for Starlette and FastAPI)."""
    if getattr(app, MARKER, False):
        return app
    # Like Django's DEBUG: on in development only (TRACEPANEL_ENABLED, set by `tracepanel run`, wins).
    values = {"enabled": bool(getattr(app, "debug", False)), **settings}
    if not _setup.enable(lambda: values):
        return app
    router = getattr(app, "router", None)
    if hasattr(app, "add_middleware") and router is not None:
        # Starlette and FastAPI: the middleware goes on the app itself, and sees its routes.
        setattr(app, MARKER, True)
        app.add_middleware(TracepanelMiddleware, routes=lambda: router.routes)
        _setup.started(type(app).__name__ if type(app).__name__ in {"FastAPI", "Starlette"} else "ASGI app")
        return app
    wrapped = TracepanelMiddleware(app)
    setattr(wrapped, MARKER, True)
    _setup.started("ASGI app")
    return wrapped
