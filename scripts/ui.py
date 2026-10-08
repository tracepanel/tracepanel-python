"""Put the built tracepanel UI (local app + widget.js) into tracepanel/static/tracepanel/.

Sources, first match wins:
1. TRACEPANEL_UI_DIST: a directory with the built app (tracepanel-ui/apps/local/dist).
2. ../tracepanel-ui/apps/local/dist: a sibling checkout of tracepanel-ui.
"""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tracepanel" / "static" / "tracepanel"
SIBLING = ROOT.parent / "tracepanel-ui" / "apps" / "local" / "dist"


def install_from(dist: Path) -> None:
    if not (dist / "index.html").is_file() or not (dist / "widget.js").is_file():
        msg = f"{dist} is not a built tracepanel UI (no index.html and widget.js): run `pnpm build` in tracepanel-ui"
        raise SystemExit(msg)
    shutil.rmtree(TARGET / "ui", ignore_errors=True)
    shutil.copytree(dist, TARGET / "ui")
    shutil.copy2(dist / "widget.js", TARGET / "widget.js")


def main() -> None:
    explicit = os.environ.get("TRACEPANEL_UI_DIST")
    dist = Path(explicit) if explicit else SIBLING
    if not explicit and not dist.is_dir():
        sys.exit(f"No built UI: set TRACEPANEL_UI_DIST or build tracepanel-ui next to this repository ({dist})")
    install_from(dist)
    print(f"tracepanel UI installed from {dist}", file=sys.stderr)  # noqa: T201


if __name__ == "__main__":
    main()
