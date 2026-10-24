"""The server's access log without the panel's own requests: the widget polls the JSON API several times a second.

A filter on each server's access logger drops the records whose request path is under `prefix`; the app's own requests
stay. A filter on the logger object outlives the logging setup a server does after tracepanel's (uvicorn, gunicorn,
granian and Django apply theirs with dictConfig, which replaces a logger's handlers but keeps its filters). It runs only
for the access loggers' records, never on the app's own logging.

Each server hands the path in its record's arguments, never only in the formatted message:

- a mapping: gunicorn and hypercorn as the "U" atom, granian and Channels' runserver as "path";
- a tuple: uvicorn the path (with its query), Django's runserver and Flask's (werkzeug) the request line
  ("GET /path HTTP/1.1", werkzeug's in ANSI colors for some statuses).

Daphne on its own writes its access log to a stream, not through logging: its writer is patched to skip them.
"""

import logging
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from tracepanel._compat import override
from tracepanel.core import conf, imports
from tracepanel.core.patching import original, patch

LOGGERS = (
    "uvicorn.access",
    "gunicorn.access",
    "hypercorn.access",
    "granian.access",
    # Django's runserver, Channels' (daphne's) runserver, Flask's development server.
    "django.server",
    "django.channels.server",
    "werkzeug",
)
# Where a mapping of arguments has the request's path: gunicorn's and hypercorn's atoms, granian's and Channels'.
PATH_KEYS = ("U", "path")


def ours(path: str, base: str) -> bool:
    """Whether a request path (with its query or the rest of a request line) is under base, the prefix's."""
    return bool(base) and path.startswith(base) and path[len(base) : len(base) + 1] in {"", "/", "?", " "}


def _base() -> str:
    return conf.get_config().prefix.rstrip("/")


def _path(arg: str) -> str:
    """The path in an argument: itself, or the second word of a request line."""
    if arg.startswith("/"):
        return arg
    _, _, rest = arg.partition(" ")
    return rest


class QuietAccessLog(logging.Filter):
    @override
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, Mapping):
            base = _base()
            return not any(isinstance(path := args.get(key), str) and ours(path, base) for key in PATH_KEYS)
        if isinstance(args, tuple):
            base = _base()
            return not any(isinstance(arg, str) and ours(_path(arg), base) for arg in args)
        return True


_filter = QuietAccessLog()


def _daphne(module: ModuleType) -> None:
    generator = module.AccessLogGenerator

    def call(self: Any, protocol: str, action: str, details: Mapping[str, object]) -> None:  # noqa: ANN401
        path = details.get("path")
        if isinstance(path, str) and conf.get_config().quiet_access_log and ours(path, _base()):
            return
        original(generator, "__call__")(self, protocol, action, details)

    patch(generator, "__call__", call)


def install() -> None:
    """Filter the servers' access loggers, unless `quiet_access_log` is off."""
    if not conf.get_config().quiet_access_log:
        return
    for name in LOGGERS:
        # addFilter adds a filter once.
        logging.getLogger(name).addFilter(_filter)
    imports.when_imported("daphne.access", _daphne)


def uninstall() -> None:
    """Take the filter off again (for tests)."""
    for name in LOGGERS:
        logging.getLogger(name).removeFilter(_filter)
