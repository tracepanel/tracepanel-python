"""What `tracepanel run <command>` turns on, without changes to the project's code.

Each framework and library is patched when the project imports it (an import hook), so a process that never imports
FastAPI never loads it: Django gets `tracepanel.django` in INSTALLED_APPS, a Starlette, FastAPI, Flask or Falcon app
gets the middleware when it is created. A process with no framework (a script, a worker) gets the libraries all the
same: the database drivers, HTTP clients, Redis, MongoDB, task queues, SQLAlchemy and logging, each patched once the
project imports it.
"""

import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any

from tracepanel.core import conf, imports

MARKER = "_tracepanel"


def _django(module: ModuleType) -> None:
    """Add tracepanel.django to INSTALLED_APPS as Django reads the settings."""
    settings_class = module.Settings
    original = settings_class.__init__

    def init(self: Any, *args: object, **kwargs: object) -> None:  # noqa: ANN401 (django.conf.Settings)
        original(self, *args, **kwargs)
        apps = list(self.INSTALLED_APPS)
        if "tracepanel.django" not in apps:
            self.INSTALLED_APPS = [*apps, "tracepanel.django"]

    settings_class.__init__ = init


def _fastapi(module: ModuleType) -> None:
    """Install tracepanel on every FastAPI app as it is created (its routes are wrapped as they are added)."""
    app_class = module.FastAPI
    original = app_class.__init__

    def init(self: Any, *args: object, **kwargs: object) -> None:  # noqa: ANN401 (fastapi.FastAPI)
        original(self, *args, **kwargs)
        from tracepanel import fastapi  # noqa: PLC0415 (only once a FastAPI app exists)

        fastapi.install(self)

    app_class.__init__ = init


def _starlette(module: ModuleType) -> None:
    """Install tracepanel on every Starlette app as it is created (FastAPI skips this init: it has its own hook)."""
    app_class = module.Starlette
    original = app_class.__init__

    def init(self: Any, *args: object, **kwargs: object) -> None:  # noqa: ANN401 (starlette.applications.Starlette)
        original(self, *args, **kwargs)
        from tracepanel import asgi  # noqa: PLC0415 (only once a Starlette app exists)

        asgi.install(self)

    app_class.__init__ = init


def _falcon(module: ModuleType) -> None:
    """Install tracepanel on every Falcon app (WSGI or ASGI) as it is created, in place: the app stays the one served.

    falcon.asgi.App's own __init__ calls this one first: install() sees an ASGI app all the same.
    """
    app_class = module.App
    original = app_class.__init__

    def init(self: Any, *args: object, **kwargs: object) -> None:  # noqa: ANN401 (falcon.App, falcon.asgi.App)
        original(self, *args, **kwargs)
        from tracepanel import falcon  # noqa: PLC0415 (only once a Falcon app exists)

        falcon.install(self)

    app_class.__init__ = init


def _flask(module: ModuleType) -> None:
    """Install tracepanel on every Flask app as it is created (it still only traces in debug mode or under run)."""
    app_class = module.Flask
    original = app_class.__init__

    def init(self: Any, *args: object, **kwargs: object) -> None:  # noqa: ANN401 (flask.Flask)
        original(self, *args, **kwargs)
        from tracepanel import flask  # noqa: PLC0415 (only once a Flask app exists)

        flask.install(self)

    app_class.__init__ = init


# Module -> what to patch once it has loaded. Hook packages rather than their submodules where the patch needs the
# package finished: a submodule loads while its package is still importing.
HOOKS: dict[str, Callable[[ModuleType], None]] = {
    "django.conf": _django,
    "fastapi.applications": _fastapi,
    "starlette.applications": _starlette,
    "flask.app": _flask,
    # The package: falcon.app loads while falcon (and falcon.asgi, which install() checks for) is still importing.
    "falcon": _falcon,
}


def install() -> None:
    """Hooks for what the project imports later, and what needs no framework: logging, the terminal line."""
    if getattr(sys, MARKER, False):
        return
    setattr(sys, MARKER, True)
    for name, hook in HOOKS.items():
        # Already imported (rare, before sitecustomize): patched now.
        imports.when_imported(name, hook)
    if conf.get_config().enabled:
        from tracepanel.console import terminal  # noqa: PLC0415
        from tracepanel.core import registry  # noqa: PLC0415
        from tracepanel.integrations import logging  # noqa: PLC0415

        logging.install()
        # Every library, each patched once the project imports it: a script or a worker has no framework to do it.
        registry.install()
        terminal.install()
