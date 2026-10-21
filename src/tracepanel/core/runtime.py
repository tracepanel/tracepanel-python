"""What a trace ran on: the language, its runtime and the web framework, named as OpenTelemetry names them.

The panel shows it as a short label ("Python 3.12 · Django 6.0"), so a page served by a Go service next to a Django
one says which is which.
"""

import platform
import sys
from importlib import metadata

from tracepanel.core.model import Attrs

# Framework names as tracepanel's integrations report them -> the distribution that has the version.
DISTRIBUTIONS = {
    "Django": "django",
    "FastAPI": "fastapi",
    "Starlette": "starlette",
    "Flask": "flask",
    "Falcon": "falcon",
}

_framework: str | None = None
_named: tuple[str, str | None] | None = None


def set_framework(name: str) -> None:
    """The web framework this process serves with, and its version when it is a known one."""
    global _framework, _named  # noqa: PLW0603 (one per process)
    distribution = DISTRIBUTIONS.get(name)
    if distribution is None:
        _framework = _named = None
        return
    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        _framework, _named = name, (name, None)
        return
    _framework, _named = f"{name} {version}", (name, version)


def framework() -> tuple[str, str | None] | None:
    """The known web framework this process serves with and its version: ("Django", "6.0.1")."""
    return _named


def attrs() -> Attrs:
    return {
        "telemetry.sdk.language": "python",
        "process.runtime.name": sys.implementation.name,
        "process.runtime.version": platform.python_version(),
        **({"tracepanel.framework": _framework} if _framework else {}),
    }


def _short(version: str) -> str:
    return ".".join(version.split(".")[:2])


def label() -> str:
    """The short form the panel shows: "Python 3.12 · Django 6.0"."""
    python = f"Python {_short(platform.python_version())}"
    if _framework is None:
        return python
    name, _, version = _framework.partition(" ")
    return f"{python} · {name} {_short(version)}" if version else f"{python} · {name}"
