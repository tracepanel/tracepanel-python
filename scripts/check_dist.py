"""Fail if a built wheel or sdist in dist/ holds what belongs to the agent: the console's UI, the store.

tracepanel is the SDK only; the UI, widget.min.js and the store come with tracepanel-agent, which is closed.
"""

import sys
import tarfile
import zipfile
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent / "dist"
FORBIDDEN = ("tracepanel/static/", ".sqlite3", "widget.min.js", "schema.sql")


def names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            return wheel.namelist()
    with tarfile.open(path) as sdist:
        # sdist members are prefixed with "tracepanel-<version>/".
        return [name.split("/", 1)[1] for name in sdist.getnames() if "/" in name]


def main() -> None:
    artifacts = [*DIST.glob("*.whl"), *DIST.glob("*.tar.gz")]
    if not artifacts:
        sys.exit("dist/ is empty: run `uv build` first")
    for artifact in artifacts:
        found = [name for name in names(artifact) if any(part in name for part in FORBIDDEN)]
        if found:
            sys.exit(f"{artifact.name} holds the agent's files: {', '.join(found[:5])}")
        print(f"{artifact.name}: SDK only", file=sys.stderr)  # noqa: T201


if __name__ == "__main__":
    main()
