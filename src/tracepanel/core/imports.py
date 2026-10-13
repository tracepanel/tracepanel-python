"""Patching a library when the app imports it, not before.

tracepanel knows many libraries; an app uses a few of them. Importing every one that is installed, to patch it at
startup, would load modules the app never uses: seconds of startup in a large environment, and a bigger heap for the
garbage collector to walk on every collection. when_imported() runs a hook on the module right away when it is already
imported, else right after the app imports it (an import hook on sys.meta_path).

A hook runs once per module, after the module's own code: hook a package rather than a submodule when the patch needs
the whole package (a submodule loads while its package is still importing).
"""

import importlib.abc
import importlib.machinery
import sys
import threading
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Any

from tracepanel._compat import override

Hook = Callable[[ModuleType], None]

_waiting: dict[str, list[Hook]] = {}
# Modules whose own code has run: their hooks are running, or have (a hook added by one of them runs at once).
_loaded: set[str] = set()
_lock = threading.Lock()


def when_imported(name: str, hook: Hook) -> None:
    """Run hook(module) once the module `name` is imported: now, when it already is."""
    module = sys.modules.get(name)
    if module is not None and _initialized(name, module):
        _run(name, module, [hook])
        return
    with _lock:
        _waiting.setdefault(name, []).append(hook)
        _install()
    # Imported in another thread meanwhile: its loader may have run before the hook was waiting.
    module = sys.modules.get(name)
    if module is not None and _initialized(name, module):
        _fire(name, module)


def _initialized(name: str, module: ModuleType) -> bool:
    """False while the module's own code is still running (a circular import): its hook waits for the loader."""
    spec = getattr(module, "__spec__", None)
    return name in _loaded or not getattr(spec, "_initializing", False)


def _fire(name: str, module: ModuleType) -> None:
    with _lock:
        hooks = _waiting.pop(name, [])
    _run(name, module, hooks)


def _run(name: str, module: ModuleType, hooks: list[Hook]) -> None:
    for hook in hooks:
        try:
            hook(module)
        except Exception as exc:  # noqa: BLE001 (a failed patch must not break the app's import)
            sys.stderr.write(f"tracepanel: could not patch {name}: {exc}\n")


class _Loader(importlib.abc.Loader):
    """The module's own loader, then the hooks waiting for it; anything else asked of it (resources) is the loader's."""

    def __init__(self, loader: importlib.abc.Loader, name: str) -> None:
        self._loader = loader
        self._name = name

    @override
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self._loader.create_module(spec)

    @override
    def exec_module(self, module: ModuleType) -> None:
        self._loader.exec_module(module)
        _loaded.add(self._name)
        _fire(self._name, module)

    def __getattr__(self, attribute: str) -> Any:  # noqa: ANN401 (whatever the loader has)
        return getattr(self._loader, attribute)


class _Finder(importlib.abc.MetaPathFinder):
    """Finds nothing itself: the module's spec from the other finders, its loader wrapped when a hook waits for it."""

    @override
    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in _waiting:
            return None
        for finder in sys.meta_path:
            if isinstance(finder, _Finder) or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None:
                continue
            if spec.loader is not None:
                spec.loader = _Loader(spec.loader, fullname)
            return spec
        return None


def _install() -> None:
    if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
