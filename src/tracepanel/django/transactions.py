"""Transactions: a span per atomic() block (a transaction, or a savepoint when nested) and per on_commit() callback.

Django runs on_commit callbacks inside Atomic.__exit__, right after COMMIT: the transaction span ends at the commit,
and the callbacks become its siblings, so a slow callback is not counted as time the transaction held its locks.

A block may close in another context than the one it opened in: FastAPI enters a sync dependency's `with atomic():`
in one worker thread's copy of the request's context and leaves it in another's. The open blocks are kept on the
connection they run on, as Django keeps them.
"""

import functools
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.transaction import Atomic

from tracepanel.core.context import OpenSpan, current_trace, open_span, start_span
from tracepanel.core.frames import origin
from tracepanel.core.patching import patch, patched
from tracepanel.django import services as django_services
from tracepanel.django.signals import qualname

if TYPE_CHECKING:
    from tracepanel.core.model import Attrs


@dataclass(slots=True)
class _Open:
    """An atomic block being timed (None: the trace had no room left for its span)."""

    atomic: Atomic
    opened: OpenSpan | None
    committed: int | None = None


# The atomic blocks timed on a connection, innermost last: Atomic instances are reused as decorators.
_OPEN = "_tracepanel_atomic"


def _open(connection: BaseDatabaseWrapper) -> list[_Open]:
    stack: list[_Open] | None = getattr(connection, _OPEN, None)
    if stack is None:
        stack = []
        setattr(connection, _OPEN, stack)
    return stack


def _connection(atomic: Atomic) -> BaseDatabaseWrapper:
    return connections[atomic.using or DEFAULT_DB_ALIAS]


def _enter(atomic: Atomic) -> None:
    using = atomic.using or DEFAULT_DB_ALIAS
    connection = connections[using]
    nested = connection.in_atomic_block
    attrs: Attrs = {
        "db.alias": using,
        **django_services.for_connection(connection),
        "db.transaction.savepoint": nested,
        "db.transaction.durable": bool(getattr(atomic, "durable", False)),
        "code.origin": origin(),
    }
    opened = open_span("transaction", f"{'savepoint' if nested else 'transaction'} {using}", attrs)
    _open(connection).append(_Open(atomic, opened))


def _exit(atomic: Atomic, exc_type: type[BaseException] | None, failed: bool) -> None:  # noqa: FBT001
    connection = _connection(atomic)
    entry = _open(connection).pop()
    opened = entry.opened
    if opened is None:
        return
    rolled_back = failed or exc_type is not None or connection.needs_rollback
    opened.span.attrs["db.transaction.outcome"] = "rollback" if rolled_back else "commit"
    if failed or exc_type is not None:
        opened.span.status = "error"
    # In whatever context it ends (another worker thread than the one it began in): its parent is current again.
    opened.end()
    if entry.committed is not None:
        # The block ends at COMMIT: on_commit callbacks ran after it.
        opened.span.duration = (entry.committed - opened.started) // 1000


def _on_commit_callback(func: Any, connection: BaseDatabaseWrapper) -> Any:  # noqa: ANN401 (any callable)
    """Time the callback as a sibling of the transaction it waited for."""
    stack = _open(connection)
    opened = stack[-1].opened if stack else None
    parent_id = opened.span.parent_id if opened is not None else None
    alias = connection.alias

    @functools.wraps(func)
    def run() -> object:
        attrs: Attrs = {"code.function": qualname(func), "db.alias": alias, "db.transaction.on_commit": True}
        with start_span("function", f"on_commit {qualname(func)}", attrs) as span:
            if span is not None and parent_id is not None:
                span.parent_id = parent_id
            return func()

    return run


# Django's own methods, kept before they are patched.
_atomic_enter, _atomic_exit = Atomic.__enter__, Atomic.__exit__
_on_commit = BaseDatabaseWrapper.on_commit
_run_commit_hooks = BaseDatabaseWrapper.run_and_clear_commit_hooks


def _patched_enter(self: Atomic) -> None:
    if current_trace() is None:
        _atomic_enter(self)
        return
    _enter(self)
    try:
        _atomic_enter(self)
    except BaseException:
        # BEGIN failed (no connection): no block to time, and none left open on the connection.
        _exit(self, None, failed=True)
        raise


def _patched_exit(self: Atomic, exc_type: Any, exc_value: Any, traceback: Any) -> None:  # noqa: ANN401 (Django's)
    stack = _open(_connection(self))
    if not stack or stack[-1].atomic is not self:
        # Entered outside a trace.
        return _atomic_exit(self, exc_type, exc_value, traceback)
    try:
        _atomic_exit(self, exc_type, exc_value, traceback)
    except BaseException:
        # COMMIT itself failed (a deferred constraint, a lost connection).
        _exit(self, exc_type, failed=True)
        raise
    _exit(self, exc_type, failed=False)
    return None


def _patched_on_commit(self: BaseDatabaseWrapper, func: Any, robust: bool = False) -> None:  # noqa: ANN401, FBT001, FBT002
    if current_trace() is None:
        return _on_commit(self, func, robust)
    return _on_commit(self, _on_commit_callback(func, self), robust)


def _patched_run_commit_hooks(self: BaseDatabaseWrapper) -> None:
    stack = _open(self)
    if stack:
        stack[-1].committed = time.perf_counter_ns()
    _run_commit_hooks(self)


def install() -> None:
    if patched(Atomic, "__enter__"):
        return
    patch(Atomic, "__enter__", _patched_enter)
    patch(Atomic, "__exit__", _patched_exit)
    patch(BaseDatabaseWrapper, "on_commit", _patched_on_commit)
    patch(BaseDatabaseWrapper, "run_and_clear_commit_hooks", _patched_run_commit_hooks)
