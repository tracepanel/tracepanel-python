"""Exceptions as trace errors: the frames, the chain of causes, and exceptions a log record carried.

Integrations call capture() where their framework reports an exception (Django's got_request_exception, a failed
task, a GraphQL resolver).
"""

from tracepanel.core import conf, forks
from tracepanel.core.context import current_span, current_trace, now_us
from tracepanel.core.frames import from_traceback
from tracepanel.core.model import ChainedError, Error

MAX_CHAIN = 5


def _qualname(exc: BaseException) -> str:
    exc_type = type(exc)
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


def chain_of(exc: BaseException, *, with_locals: bool) -> list[ChainedError]:
    """Exceptions behind `exc`, nearest first, the way Python prints them bottom-up."""
    chain: list[ChainedError] = []
    seen = {id(exc)}
    current: BaseException = exc
    while len(chain) < MAX_CHAIN:
        if current.__cause__ is not None:
            relation, inner = "cause", current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            relation, inner = "context", current.__context__
        else:
            break
        if id(inner) in seen:
            break
        seen.add(id(inner))
        chain.append(
            ChainedError(
                relation=relation,
                type=_qualname(inner),
                message=str(inner),
                frames=from_traceback(inner.__traceback__, with_locals=with_locals),
            )
        )
        current = inner
    return chain


def capture(exc: BaseException, *, handled: bool) -> None:
    trace = current_trace()
    if trace is None:
        return
    span = current_span()
    with_locals = conf.get_config().capture_locals
    trace.errors.append(
        Error(
            span_id=span.id if span else None,
            time=now_us(),
            type=_qualname(exc),
            message=str(exc),
            handled=handled,
            frames=from_traceback(exc.__traceback__, with_locals=with_locals),
            chain=chain_of(exc, with_locals=with_locals),
        )
    )
    trace.status = "error"


# The latest exception logged in each open trace (logger.exception): when a request ends up a 5xx with no
# exception of its own, a framework caught and logged it (django-ninja, custom handlers), so that is the error.
_logged: dict[str, BaseException] = {}
_logged_lock = forks.lock()


def remember_logged(trace_id: str, exc: BaseException) -> None:
    with _logged_lock:
        _logged[trace_id] = exc


def pop_logged_exception(trace_id: str) -> BaseException | None:
    with _logged_lock:
        return _logged.pop(trace_id, None)
