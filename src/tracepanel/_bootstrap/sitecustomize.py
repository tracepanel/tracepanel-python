"""Loaded by Python before the project's code when started with `tracepanel run` (this directory is on PYTHONPATH).

Installs tracepanel's import hooks, then runs the project's own sitecustomize, if it has one, as Python would have.
It may run under any interpreter the command names, one without tracepanel too: whatever goes wrong here, the
process starts with one line on stderr. Every module it uses is imported explicitly, never assumed loaded.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

try:
    from tracepanel import _run

    _run.install()
except Exception as exc:  # noqa: BLE001 (never stop the project from starting)
    sys.stderr.write(f"tracepanel: not tracing this process: {exc}\n")

# The next sitecustomize on the path (the project's or the virtualenv's) still runs.
try:
    _here = str(Path(__file__).resolve().parent)
    _rest = [entry for entry in sys.path if str(Path(entry or ".").resolve()) != _here]
    _spec = importlib.machinery.PathFinder.find_spec("sitecustomize", _rest)
except Exception as exc:  # noqa: BLE001 (never stop the project from starting)
    sys.stderr.write(f"tracepanel: could not look for the project's sitecustomize: {exc}\n")
    _spec = None
# Its own errors are its own: Python reports them as it would without tracepanel.
if _spec is not None and _spec.loader is not None:
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["sitecustomize"] = _module
    _spec.loader.exec_module(_module)
