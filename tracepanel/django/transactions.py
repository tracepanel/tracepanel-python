"""Transactions: a span per atomic() block (a transaction, or a savepoint when nested) and per on_commit() callback.

Django runs on_commit callbacks inside Atomic.__exit__, right after COMMIT: the transaction span ends at the commit,
and the callbacks become its siblings, so a slow callback is not counted as time the transaction held its locks.
"""

import functools
import time
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.transaction import Atomic

from tracepanel.core.context import current_trace, start_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, Span
from tracepanel.core.patching import patch, patched
from tracepanel.django import services as django_services
from tracepanel.django.signals import qualname


@dataclass(slots=True)
class _Open:
    """An atomic block being timed; Atomic instances are reused as decorators, so this lives in the context."""

    context: AbstractContextManager[Span | None]
    span: Span | None
    started: int
    committed: int | None = None


_open: ContextVar[tuple[_Open, ...]] = ContextVar("tracepanel_atomic", default=())


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
    context = start_span("transaction", f"{'savepoint' if nested else 'transaction'} {using}", attrs)
    _open.set((*_open.get(), _Open(context, context.__enter__(), time.perf_counter_ns())))


def _exit(atomic: Atomic, exc_type: type[BaseException] | None, failed: bool) -> None:  # noqa: FBT001
    stack = _open.get()
    if not stack:
        return
    *rest, entry = stack
    _open.set(tuple(rest))
    connection = connections[atomic.using or DEFAULT_DB_ALIAS]
    if entry.span is not None:
        rolled_back = failed or exc_type is not None or connection.needs_rollback
        entry.span.attrs["db.transaction.outcome"] = "rollback" if rolled_back else "commit"
        if failed or exc_type is not None:
            entry.span.status = "error"
    entry.context.__exit__(None, None, None)
    if entry.span is not None and entry.committed is not None:
        # The block ends at COMMIT: on_commit callbacks ran after it.
        entry.span.duration = (entry.committed - entry.started) // 1000


def _on_commit_callback(func: Any, alias: str) -> Any:  # noqa: ANN401 (any callable)
    """Time the callback as a sibling of the transaction it waited for."""
    stack = _open.get()
    parent_id = stack[-1].span.parent_id if stack and stack[-1].span is not None else None

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
    if current_trace() is not None:
        _enter(self)
    _atomic_enter(self)


def _patched_exit(self: Atomic, exc_type: Any, exc_value: Any, traceback: Any) -> None:  # noqa: ANN401 (Django's)
    if not _open.get():
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
    return _on_commit(self, _on_commit_callback(func, self.alias), robust)


def _patched_run_commit_hooks(self: BaseDatabaseWrapper) -> None:
    stack = _open.get()
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
