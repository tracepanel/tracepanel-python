import importlib
import importlib.resources
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tracepanel.core.imports import when_imported

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A package of its own, not imported yet: a name no other test uses."""
    name = f"lib_{tmp_path.name.replace('-', '_')}"
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n")
    (package / "data.txt").write_text("in the package\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    for module in [module for module in sys.modules if module == name or module.startswith(f"{name}.")]:
        del sys.modules[module]


def test_a_hook_waits_for_the_app_to_import_the_module(library: str) -> None:
    seen: list[object] = []
    when_imported(library, lambda module: seen.append(module.VALUE))

    assert seen == []
    assert library not in sys.modules

    importlib.import_module(library)

    assert seen == [1]


def test_a_module_already_imported_is_hooked_at_once(library: str) -> None:
    importlib.import_module(library)
    seen: list[object] = []

    when_imported(library, lambda module: seen.append(module.VALUE))

    assert seen == [1]


def test_a_hook_added_by_a_hook_of_the_same_module_runs(library: str) -> None:
    # tracepanel run: the library's hook installs its integration, which hooks the library again.
    seen: list[str] = []

    def first(module: ModuleType) -> None:
        seen.append("first")
        when_imported(module.__name__, lambda _: seen.append("second"))

    when_imported(library, first)
    importlib.import_module(library)

    assert seen == ["first", "second"]


def test_a_failed_hook_does_not_fail_the_import(library: str, capsys: pytest.CaptureFixture[str]) -> None:
    def failing(module: ModuleType) -> None:
        raise RuntimeError(module.__name__)

    when_imported(library, failing)

    assert importlib.import_module(library).VALUE == 1
    assert capsys.readouterr().err == f"tracepanel: could not patch {library}: {library}\n"


def test_the_module_keeps_its_resources(library: str) -> None:
    when_imported(library, lambda _: None)

    importlib.import_module(library)

    assert importlib.resources.files(library).joinpath("data.txt").read_text() == "in the package\n"


def test_a_django_process_loads_only_the_libraries_the_app_imports() -> None:
    script = """
import json, sys
import django
django.setup()
libraries = ("celery", "sqlalchemy", "strawberry", "aiohttp", "asyncpg", "opentelemetry")
loaded = {name for name in libraries if name in sys.modules}
import sqlalchemy
from sqlalchemy.engine import Engine
print(json.dumps([sorted(loaded), getattr(Engine, "_tracepanel", False)]))
"""
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "tests.project.settings", "PYTHONPATH": str(ROOT)}
    result = subprocess.run(  # noqa: S603 (this interpreter)
        [sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )

    # Nothing the app didn't import; SQLAlchemy traced once it does.
    assert result.stdout.splitlines()[-1] == "[[], true]"
