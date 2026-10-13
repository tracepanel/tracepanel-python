"""Management commands as `command` traces: `manage.py migrate`, `manage.py send_digest`, cron jobs that call them.

Long-running and interactive commands (the dev server, shells, the test runner) are left out by default; see the
`ignore_commands` setting. The trace is written before the process exits.
"""

import functools
import sys

from django.core.management.base import BaseCommand

from tracepanel.api import trace
from tracepanel.core import conf
from tracepanel.core.frames import safe_repr
from tracepanel.core.patching import patch, patched
from tracepanel.storage.transport import get_writer


def command_name(command: BaseCommand) -> str:
    """`migrate` for django.core.management.commands.migrate."""
    return type(command).__module__.rsplit(".", 1)[-1]


def install() -> None:
    if patched(BaseCommand, "execute"):
        return
    original = BaseCommand.execute

    @functools.wraps(original)
    def execute(self: BaseCommand, *args: object, **options: object) -> object:
        name = command_name(self)
        config = conf.get_config()
        if conf.matches(name, config.ignore_commands):
            return original(self, *args, **options)
        attrs = {
            "command.name": name,
            "command.argv": " ".join(sys.argv[1:]) if sys.argv[1:2] == [name] else None,
            "command.options": safe_repr(
                "options", {key: value for key, value in options.items() if value not in (None, False, [], ())}
            ),
        }
        try:
            with trace(f"manage.py {name}", kind="command", attrs=attrs):
                return original(self, *args, **options)
        finally:
            # Commands end the process: write the trace before that.
            get_writer().flush()

    patch(BaseCommand, "execute", execute)
