"""The CI test matrix on this machine: every supported Python x Django pair, each in its own environment.

    uv run poe test-matrix                     # all pairs
    uv run poe test-matrix 3.12/6.1 3.11/5.2   # some of them
    uv run poe test-matrix -- -x -k mail       # extra pytest arguments after --

Environments live in .venv-matrix/ (reused between runs); SQLite only (CI adds Postgres).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Keep in sync with .github/workflows/ci.yml.
PAIRS = [
    ("3.11", "5.2"),
    *((python, django) for python in ("3.12", "3.13", "3.14") for django in ("5.2", "6.0", "6.1")),
    ("3.15", "6.1"),
]
GROUPS = [
    "celery",
    "channels",
    "strawberry",
    "django-tasks",
    "django-q2",
    "fastapi",
    "dramatiq",
    "rq",
    "huey",
    "ninja",
    "drf",
    "flask",
    "falcon",
    "otel",
    "faststream",
]


def run(pair: tuple[str, str], pytest_args: list[str]) -> bool:
    python, django = pair
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv-matrix" / f"py{python}-django{django}")}
    env.pop("VIRTUAL_ENV", None)
    groups = [arg for group in GROUPS for arg in ("--group", group)]
    steps = [
        ["uv", "sync", "--quiet", "--locked", "--python", python, *groups],
        ["uv", "pip", "install", "--quiet", "--python", env["UV_PROJECT_ENVIRONMENT"], f"django~={django}.0"],
        ["uv", "run", "--no-sync", "--python", python, "pytest", "-q", "-p", "no:cacheprovider", *pytest_args],
    ]
    print(f"== python {python}, django {django}", flush=True)
    # Stops at the first failing step.
    return all(subprocess.run(step, cwd=ROOT, env=env, check=False).returncode == 0 for step in steps)  # noqa: S603


def main() -> None:
    args = sys.argv[1:]
    pytest_args: list[str] = args[args.index("--") + 1 :] if "--" in args else []
    wanted = {tuple(arg.split("/", 1)) for arg in (args[: args.index("--")] if "--" in args else args)}
    pairs = [pair for pair in PAIRS if not wanted or pair in wanted]
    failed = [f"{python}/{django}" for python, django in pairs if not run((python, django), pytest_args)]
    print(f"\n{len(pairs) - len(failed)} of {len(pairs)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
