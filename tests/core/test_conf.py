import os
from collections.abc import Generator
from dataclasses import fields
from pathlib import Path

import pytest

from tracepanel.core import conf, registry
from tracepanel.core.conf import ConfigError


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """A project directory with no framework settings (as under `tracepanel run`)."""
    previous = conf._framework  # noqa: SLF001
    conf.use_source(lambda: {"root": str(tmp_path / "app")})
    (tmp_path / "app").mkdir()
    for name in [n for n in os.environ if n.startswith("TRACEPANEL_")]:
        monkeypatch.delenv(name)
    yield tmp_path / "app"
    conf.use_source(previous)


def test_defaults_without_a_file(project: Path) -> None:
    assert (conf.get_config().slow_sql_ms, conf.get_config().service) == (100, "app")


def test_pyproject_section(project: Path) -> None:
    (project / "pyproject.toml").write_text('[tool.tracepanel]\nslow_sql_ms = 200\nignore_paths = ["/health*"]\n')

    config = conf.get_config()

    assert (config.slow_sql_ms, config.ignore_paths) == (200, ("/health*",))


def test_tracepanel_toml_wins_and_is_found_upward(project: Path) -> None:
    (project.parent / "tracepanel.toml").write_text('service = "shop"\n')
    (project / "sub").mkdir()
    conf.use_source(lambda: {"root": str(project / "sub")})

    assert conf.get_config().service == "shop"


def test_a_pyproject_without_the_section_ends_the_search(project: Path) -> None:
    (project.parent / "tracepanel.toml").write_text('service = "not ours"\n')
    (project / "pyproject.toml").write_text('[project]\nname = "app"\n')

    assert conf.get_config().service == "app"


def test_environment_wins_over_the_file(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "tracepanel.toml").write_text("slow_sql_ms = 200\ncapture_locals = true\n")
    monkeypatch.setenv("TRACEPANEL_SLOW_SQL_MS", "50")
    monkeypatch.setenv("TRACEPANEL_CAPTURE_LOCALS", "off")
    monkeypatch.setenv("TRACEPANEL_IGNORE_TASKS", "celery.*, beat.*")

    explained = {name: (value, origin) for name, value, origin in conf.explain()}

    assert explained["slow_sql_ms"] == (50, "env TRACEPANEL_SLOW_SQL_MS")
    assert explained["capture_locals"] == (False, "env TRACEPANEL_CAPTURE_LOCALS")
    assert explained["ignore_tasks"] == (("celery.*", "beat.*"), "env TRACEPANEL_IGNORE_TASKS")
    assert explained["n_plus_one_threshold"] == (3, "default")
    assert explained["root"] == (str(project), "framework settings")
    assert explained["service"] == ("app", "name of root")


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("TRACEPANEL_SLOW_SQL_MS", "fast", "TRACEPANEL_SLOW_SQL_MS: expected a whole number, got 'fast'"),
        (
            "TRACEPANEL_WIDGET",
            "maybe",
            "TRACEPANEL_WIDGET: expected one of 1, true, yes, on, 0, false, no, off, got 'maybe'",
        ),
    ],
)
def test_bad_variables_are_explained(
    project: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str, message: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigError, match=message):
        conf.get_config()


def test_bad_file_values_name_the_file(project: Path) -> None:
    (project / "tracepanel.toml").write_text('slow_sql_ms = "fast"\nwidgets = true\n')

    with pytest.raises(ConfigError, match=r"tracepanel.toml: unknown settings widgets"):
        conf.get_config()


def test_integrations_can_be_turned_off(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "tracepanel.toml").write_text("celery_enabled = false\n")

    assert (registry.get("celery").enabled, registry.get("drf").enabled) == (False, True)

    monkeypatch.setenv("TRACEPANEL_DRF_ENABLED", "false")
    monkeypatch.setenv("TRACEPANEL_DJANGO_TASKS_ENABLED", "0")
    conf.reset()

    assert (
        registry.get("celery").enabled,
        registry.get("drf").enabled,
        registry.get("django_tasks").enabled,
    ) == (False, False, False)


def test_settings_are_the_config_fields() -> None:
    # install(app, **settings) and configure() take these: a field added to Config is a keyword argument too.
    plain = {f.name: f.type for f in fields(conf.Config) if f.type in {bool, int, str}}
    lists = {f.name for f in fields(conf.Config)} - plain.keys()

    assert {name: kind for name, kind in conf.Settings.__annotations__.items() if name not in lists} == plain
    assert {name: conf.Settings.__annotations__[name] for name in lists} == dict.fromkeys(
        lists, list[str] | tuple[str, ...]
    )
