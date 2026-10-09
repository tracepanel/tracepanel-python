"""CLI: tracepanel dev | run | config | clear | agent | version."""

import argparse
import os
import sys

from tracepanel import __version__

USAGE = """usage: tracepanel <command>

  dev [--port 8765]                     the agent in the foreground: receives traces, serves the console
  run [--set name=value]... <command>   run a command with tracepanel on (uvicorn, celery, manage.py, ...)
  config                                the settings in effect, and where each one comes from
  clear [--yes]                         delete every stored trace, log, query, email and issue
  agent                                 the tracepanel agent
  version"""


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else None
    if command in ("version", "--version"):
        print(f"tracepanel {__version__}")
        return
    if command == "run":
        run(args[1:])
        return
    if command == "config":
        show_config()
        return
    if command == "clear":
        clear(args[1:])
        return
    if command == "agent":
        run_agent(args[1:])
    if command == "dev":
        dev(args[1:])
    print(USAGE, file=sys.stderr)
    sys.exit(2)


def run_agent(argv: list[str]) -> None:
    """Replace this process with the agent, on the store this project's processes use."""
    from tracepanel.core import conf  # noqa: PLC0415 (the CLI's commands load only what they use)
    from tracepanel.storage import agent  # noqa: PLC0415

    executable = agent.binary()
    if executable is None:
        sys.exit("tracepanel-agent not found: reinstall tracepanel, or set TRACEPANEL_AGENT_BIN")
    if "--dir" not in argv:
        # tracepanel.toml, TRACEPANEL_* and the current directory.
        argv = ["--dir", str(agent.store_dir(conf.get_config())), *argv]
    os.execv(executable, [executable, *argv])  # noqa: S606 (replaces this process with the agent)


def dev(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="tracepanel dev", description="Run the agent in the foreground: it receives traces and serves the console."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args(argv)
    run_agent(["--serve", f"{options.host}:{options.port}"])


def clear(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="tracepanel clear", description="Delete every stored trace, log, query, email and issue of this project."
    )
    parser.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--dir", help="the store directory (default: .tracepanel in the current directory)")
    options = parser.parse_args(argv)
    if options.dir:
        os.environ["TRACEPANEL_DIR"] = options.dir
    import json  # noqa: PLC0415 (only this command needs it)
    import urllib.request  # noqa: PLC0415

    from tracepanel.core import conf  # noqa: PLC0415
    from tracepanel.storage import agent  # noqa: PLC0415

    config = conf.get_config()
    if not options.yes:
        answer = input(f"Delete everything in {agent.store_dir(config)}? This can't be undone. [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            sys.exit("tracepanel clear: nothing deleted")
    # The agent holds the store: it deletes, as the console's button does.
    if not agent.ensure_started(config) or (base := agent.console_url(config)) is None:
        sys.exit(f"tracepanel clear: {agent.unavailable(config)}")
    request = urllib.request.Request(f"{base}api/store", method="DELETE")  # noqa: S310 (the agent's local address)
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (the agent's local address)
        deleted = int(json.loads(response.read())["deleted"])
    sys.stdout.write(f"tracepanel: deleted {deleted} {'trace' if deleted == 1 else 'traces'}\n")


def run(argv: list[str]) -> None:
    """Start a command with tracepanel on: its import hooks load first (sitecustomize); the code is untouched."""
    from dataclasses import fields  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from tracepanel.core.conf import Config  # noqa: PLC0415

    if not argv or argv[0] in {"-h", "--help"}:
        # The usage, not a command named --help.
        sys.stdout.write("usage: tracepanel run [--set name=value]... <command>\n")
        sys.exit(0 if argv else 2)
    names = {field.name for field in fields(Config)}
    env = dict(os.environ)
    while argv and argv[0] == "--set":
        if len(argv) < 2 or "=" not in argv[1]:  # noqa: PLR2004
            sys.exit("tracepanel run: --set takes name=value")
        name, value = argv[1].split("=", 1)
        if name not in names:
            sys.exit(f"tracepanel run: unknown setting {name!r} (see `tracepanel config`)")
        env[f"TRACEPANEL_{name.upper()}"] = value
        argv = argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        sys.exit("usage: tracepanel run [--set name=value]... <command> [args...]")
    env.setdefault("TRACEPANEL_ENABLED", "1")
    bootstrap = str(Path(__file__).resolve().parent / "_bootstrap")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [bootstrap, env.get("PYTHONPATH")]))
    try:
        os.execvpe(argv[0], argv, env)  # noqa: S606 (runs the user's own command)
    except FileNotFoundError:
        sys.exit(f"tracepanel run: command not found: {argv[0]}")


def show_config() -> None:
    from tracepanel.core import conf  # noqa: PLC0415

    try:
        rows = conf.explain()
    except conf.ConfigError as exc:
        sys.exit(f"tracepanel config: {exc}")
    width = max(len(name) for name, _, _ in rows)
    for name, value, origin in rows:
        if isinstance(value, tuple):
            shown: object = ", ".join(value)
        else:
            shown = str(value).lower() if isinstance(value, bool) else value
        sys.stdout.write(f"{name:<{width}}  {shown!s:<40}  {origin}\n")
    # What the app traces when it starts: an integration needs its library installed and its setting on.
    sys.stdout.write("\nintegrations\n")
    from tracepanel.core import registry  # noqa: PLC0415

    for integration in registry.INTEGRATIONS:
        available, enabled = integration.available, integration.enabled
        state = "on" if available and enabled else "off: not installed" if not available else "off: "
        state += "" if available == enabled or not available else f"{integration.name}_enabled"
        where = " (Django apps only)" if integration.django_only and state == "on" else ""
        sys.stdout.write(f"  {integration.label:<14}  {state}{where}\n")
