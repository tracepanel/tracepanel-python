"""Locates the tracepanel-agent binary installed by the tracepanel-agent package."""

import shutil
import sys
import sysconfig
from pathlib import Path

AGENT = "tracepanel-agent" + (".exe" if sys.platform == "win32" else "")


def find_agent_bin() -> str | None:
    candidates = [Path(sysconfig.get_path("scripts")), Path(sys.executable).parent]
    if user_scheme := sysconfig.get_preferred_scheme("user"):
        candidates.append(Path(sysconfig.get_path("scripts", scheme=user_scheme)))
    for directory in candidates:
        path = directory / AGENT
        if path.is_file():
            return str(path)
    return shutil.which(AGENT)
