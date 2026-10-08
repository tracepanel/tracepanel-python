"""What `tracepanel run <command>` turns on, without changes to the project's code.

Each framework and library is patched when the project imports it (an import hook), so a process that never imports
FastAPI never loads it: Django gets `tracepanel.django` in INSTALLED_APPS, a Starlette, FastAPI, Flask or Falcon app
gets the middleware when it is created, and Celery, SQLAlchemy, Strawberry and logging are traced on their own (a
worker needs no framework).
"""

import importlib.abc
import importlib.machinery
import sys
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Any

from tracepanel._compat import override
from tracepanel.core import conf

MARKER = "_tracepanel"


def _django(module: ModuleType) -> None:
    """Add tracepanel.django to INSTALLED_APPS as Django reads the settings (it still only runs under DEBUG)."""
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
    """Install tracepanel on every Falcon WSGI app as it is created (in place: the app stays the object to serve)."""
    app_class = module.App
    original = app_class.__init__

    def init(self: Any, *args: object, **kwargs: object) -> None:  # noqa: ANN401 (falcon.App)
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


def _library(name: str) -> Callable[[ModuleType], None]:
    def install(module: ModuleType) -> None:
        del module
        from tracepanel.core import registry  # noqa: PLC0415 (only once the library is imported)

        registry.get(name).install()

    return install


# Module -> what to patch once it has loaded. Hook packages rather than their submodules where the patch needs the
# package finished: a submodule loads while its package is still importing.
HOOKS: dict[str, Callable[[ModuleType], None]] = {
    "django.conf": _django,
    "fastapi.applications": _fastapi,
    "starlette.applications": _starlette,
    "flask.app": _flask,
    # The package: falcon.app loads while falcon (and falcon.asgi, which install() checks for) is still importing.
    "falcon": _falcon,
    "celery": _library("celery"),
    "dramatiq": _library("dramatiq"),
    "rq": _library("rq"),
    # The package, not sqlalchemy.engine: its events need the whole package loaded (sqlalchemy.util.preloaded).
    "sqlalchemy": _library("sqlalchemy"),
    "strawberry.schema.schema": _library("strawberry"),
}


class _Loader(importlib.abc.Loader):
    """The module's own loader, then the hook."""

    def __init__(self, loader: importlib.abc.Loader, hook: Callable[[ModuleType], None]) -> None:
        self.loader = loader
        self.hook = hook

    @override
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self.loader.create_module(spec)

    @override
    def exec_module(self, module: ModuleType) -> None:
        self.loader.exec_module(module)
        try:
            self.hook(module)
        except Exception as exc:  # noqa: BLE001 (a failed patch must not break the import)
            sys.stderr.write(f"tracepanel: could not patch {module.__name__}: {exc}\n")


class _Finder(importlib.abc.MetaPathFinder):
    @override
    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        hook = HOOKS.get(fullname)
        if hook is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _Loader(spec.loader, hook)
                return spec
        return None


def install() -> None:
    """Hooks for what the project imports later, and what needs no framework: logging, the terminal line."""
    if getattr(sys, MARKER, False):
        return
    setattr(sys, MARKER, True)
    sys.meta_path.insert(0, _Finder())
    for name, hook in HOOKS.items():
        # Already imported (rare, before sitecustomize): patch it now.
        if name in sys.modules:
            hook(sys.modules[name])
    if conf.get_config().enabled:
        from tracepanel.console import terminal  # noqa: PLC0415
        from tracepanel.integrations import logging  # noqa: PLC0415

        logging.install()
        terminal.install()
