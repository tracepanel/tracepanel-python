import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def project(path: Path) -> dict[str, object]:
    data = tomllib.loads(path.read_text())
    table: dict[str, object] = data["project"]
    return table


def test_django_is_an_extra_not_a_requirement() -> None:
    tracepanel = project(ROOT / "pyproject.toml")
    requirements = [Requirement(line) for line in tracepanel["dependencies"]]  # type: ignore[union-attr]

    # Only the agent, at tracepanel's own version: they are released together.
    assert [(r.name, str(r.specifier)) for r in requirements] == [("tracepanel-agent", f"=={tracepanel['version']}")]
    assert tracepanel["optional-dependencies"]["django"] == ["django>=5.2"]  # type: ignore[index]


def test_the_agent_is_required_where_it_has_wheels_only() -> None:
    # Wheels only, no sdist yet: elsewhere tracepanel installs without it rather than failing.
    marker = Requirement(project(ROOT / "pyproject.toml")["dependencies"][0]).marker  # type: ignore[index]
    platforms = {
        ("linux", machine): True
        for machine in ("x86_64", "aarch64", "armv7l", "armv6l", "i686", "ppc64le", "s390x", "riscv64")
    } | {
        ("darwin", "arm64"): True,
        ("darwin", "x86_64"): True,
        ("win32", "AMD64"): True,
        ("win32", "ARM64"): True,
        ("win32", "x86"): True,
        ("freebsd14", "amd64"): False,
        ("linux", "mips64"): False,
        ("emscripten", "wasm32"): False,
    }

    assert marker is not None
    assert {
        platform: marker.evaluate({"sys_platform": platform[0], "platform_machine": platform[1]})
        for platform in platforms
    } == platforms
