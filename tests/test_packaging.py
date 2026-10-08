import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project(path: Path) -> dict[str, object]:
    data = tomllib.loads(path.read_text())
    table: dict[str, object] = data["project"]
    return table


def test_django_is_an_extra_not_a_requirement() -> None:
    tracepanel = project(ROOT / "pyproject.toml")

    assert tracepanel["dependencies"] == []
    assert tracepanel["optional-dependencies"]["django"] == ["django>=5.2"]  # type: ignore[index]
