"""Sync code in worker threads: the span it leaves current stays current for the code that awaited it.

Starlette and FastAPI run a sync endpoint, and each sync dependency, through anyio.to_thread.run_sync: in a worker
thread, in a copy of the request's context. A `with` block's span ends where it began, so that copy is enough. A
transaction is not a block: SQLAlchemy's session begins one in a dependency and commits it in the endpoint. Its span
is current from BEGIN on, but only in the dependency's copy, so the endpoint's calls would be its siblings.

The awaiting code waits for the thread: the call is sequential, as if made in place. The span current when the thread's
work ends becomes current in the awaiting context, as it would be had the call run there: the transaction then holds
what the request does until COMMIT, which makes its parent current again. Concurrent tasks have contexts of their own,
so a transaction never takes in another task's calls.

A profiled request follows its work into the thread too: the endpoint's frames are there, while the event loop's thread
waits in its selector.
"""

from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any

from tracepanel.core import imports, profile
from tracepanel.core.context import current_span, current_trace, set_current_span
from tracepanel.core.patching import patch, patched

if TYPE_CHECKING:
    from tracepanel.core.model import Span


def install() -> None:
    """Patch anyio.to_thread.run_sync once anyio is imported (Starlette imports it)."""
    imports.when_imported("anyio.to_thread", _patch)


def _patch(module: ModuleType) -> None:
    if patched(module, "run_sync"):
        return
    original: Callable[..., Any] = module.run_sync

    async def run_sync(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (any callable)
        if current_trace() is None:
            return await original(func, *args, **kwargs)
        before = current_span()
        # The span current in the thread's context when its work ended (nothing if it never ran: cancelled).
        ended: list[Span | None] = []

        def call(*call_args: Any) -> Any:  # noqa: ANN401
            try:
                return func(*call_args)
            finally:
                ended.append(current_span())

        try:
            return await original(profile.followed(call), *args, **kwargs)
        finally:
            if ended and ended[0] is not before:
                set_current_span(ended[0])

    patch(module, "run_sync", run_sync)
