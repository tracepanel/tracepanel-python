"""Fail if a built wheel or sdist in dist/ is missing the bundled UI (uv_build has no build hooks)."""

import sys
import tarfile
import zipfile
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent / "dist"
REQUIRED = ("tracepanel/static/tracepanel/widget.js", "tracepanel/static/tracepanel/ui/index.html")


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
        missing = [path for path in REQUIRED if path not in names(artifact)]
        if missing:
            sys.exit(f"{artifact.name} is missing the UI ({', '.join(missing)}): run `poe ui` before building")
        print(f"{artifact.name}: UI included", file=sys.stderr)  # noqa: T201


if __name__ == "__main__":
    main()
