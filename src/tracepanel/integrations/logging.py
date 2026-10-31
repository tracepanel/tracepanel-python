"""Log records of the current trace, bound to the current span.

tracepanel adds no handler to any logger: it sees each record as the logger hands it to its handlers
(Logger.callHandlers), so the project's logging setup (basicConfig, Django's LOGGING, Celery taking over the root
logger) works as without tracepanel, and a record is captured once, whichever loggers and handlers it goes through.
"""

import logging
import re
import traceback

from tracepanel._compat import override
from tracepanel.core import conf, errors
from tracepanel.core.context import current_span, current_trace
from tracepanel.core.frames import relative, safe_repr
from tracepanel.core.masking import MASK, counted, sensitive
from tracepanel.core.model import Attrs, Log, Trace
from tracepanel.core.patching import patch, patched

MAX_LOGS = 1000
# tracepanel's own records, SQL echo, and the task queues' per-task bookkeeping ("Task x succeeded in 0.1s"), which the
# task trace already shows as its state and duration.
IGNORED = (
    "tracepanel",
    "django.db.backends",
    "celery.app.trace",
    "celery.worker",
    # Dramatiq's worker bookkeeping ("returned a value … no Results middleware").
    "dramatiq.worker",
    # RQ's ("default: tasks.count(3) (id)", "Job OK", "enqueued for retry").
    "rq.worker",
    "rq.job",
    # Taskiq's ("Executing task x with ID: …", "Exception found while executing function.", "Retrying").
    "taskiq.receiver",
    "taskiq.retry_middleware",
    "taskiq.smart_retry_middleware",
    # django-q's ("Processed 'tasks.count' (alpha-bravo)", "Enqueued [default] 1").
    "django-q",
    # Server access logs ("GET /items/1 200", "WebSocket CONNECT /ws/…"): the trace itself says it.
    "django.channels.server",
    "daphne",
    "uvicorn.access",
    "gunicorn.access",
    "hypercorn.access",
)
# The levels every SDK speaks (the trace schema's), lowest first, each from the logging number it starts at. A
# library's own level (loguru's TRACE and SUCCESS, a custom NOTICE) falls in by its number, never by its name.
LEVELS = (
    (logging.CRITICAL, "critical"),
    (logging.ERROR, "error"),
    (logging.WARNING, "warning"),
    (logging.INFO, "info"),
)


def level(number: int) -> str:
    """The schema's level for a logging level number: the highest one it reaches, debug below info."""
    return next((name for floor, name in LEVELS if number >= floor), "debug")


# Attributes every LogRecord has; anything else came from `extra=`.
STANDARD = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_KEY = re.compile(r"[^a-z0-9_]+")


def record_attrs(record: logging.LogRecord) -> Attrs:
    attrs: Attrs = {
        "code.function": record.funcName,
        "code.filepath": relative(record.pathname),
        "code.lineno": record.lineno,
        "thread.name": record.threadName,
        "process.pid": record.process,
    }
    if record.args:
        attrs["log.template"] = str(record.msg)
    for name, value in vars(record).items():
        if name in STANDARD or name.startswith("_"):
            continue
        key = f"extra.{_KEY.sub('_', name.lower()).strip('_') or 'value'}"
        if sensitive(name) and not counted(name, value):
            attrs[key] = MASK
        elif isinstance(value, (str, int, float, bool)) or value is None:
            attrs[key] = value
        else:
            attrs[key] = safe_repr(name, value)
    return attrs


def keep(trace: Trace, log: Log) -> None:
    """Add the record to the trace, or count it as dropped past MAX_LOGS."""
    if len(trace.logs) >= MAX_LOGS:
        dropped = trace.attrs.get("tracepanel.dropped_logs")
        trace.attrs["tracepanel.dropped_logs"] = (dropped if isinstance(dropped, int) else 0) + 1
        return
    trace.logs.append(log)


class TracepanelHandler(logging.Handler):
    @staticmethod
    def ignored(name: str) -> bool:
        """Left out by ignore_loggers (with its children)."""
        return any(name == prefix or name.startswith(f"{prefix}.") for prefix in conf.get_config().ignore_loggers)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        trace = current_trace()
        if trace is None or record.name.startswith(IGNORED) or self.ignored(record.name):
            return
        if len(trace.logs) >= MAX_LOGS:
            dropped = trace.attrs.get("tracepanel.dropped_logs")
            trace.attrs["tracepanel.dropped_logs"] = (dropped if isinstance(dropped, int) else 0) + 1
            return
        try:
            span = current_span()
            exc_info = record.exc_info
            exception = "".join(traceback.format_exception(exc_info[1])) if exc_info and exc_info[1] else None
            if exc_info and exc_info[1]:
                errors.remember_logged(trace.id, exc_info[1])
            trace.logs.append(
                Log(
                    span_id=span.id if span else None,
                    time=int(record.created * 1_000_000),
                    level=level(record.levelno),
                    logger=record.name,
                    message=record.getMessage(),
                    exception=exception,
                    attrs=record_attrs(record),
                )
            )
        except Exception:  # noqa: BLE001 (logging must never raise into the application)
            self.handleError(record)


_handler = TracepanelHandler()


def install() -> None:
    if patched(logging.Logger, "callHandlers"):
        return
    original = logging.Logger.callHandlers

    def call_handlers(self: logging.Logger, record: logging.LogRecord) -> None:
        original(self, record)
        _handler.handle(record)

    patch(logging.Logger, "callHandlers", call_handlers)
