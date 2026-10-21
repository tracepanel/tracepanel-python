"""Async: keep the place in project code across sync_to_async().

`await Post.objects.acount()` runs the query in a worker thread; that thread's stack starts in asgiref, so the SQL
span would have no `code.origin` and N+1 / duplicate detection (which only counts project code) would miss it.
While SyncToAsync.__call__ starts, the awaiting coroutines are still on the stack: their place is recorded in a
context variable, which asgiref copies into the thread.

A profiled request follows its work into that thread: under an ASGI server a sync view runs there (Django adapts it
with sync_to_async), while the event loop's thread waits in its selector.
"""

import copy
import functools
from typing import Any

from asgiref.sync import SyncToAsync

from tracepanel.core import profile
from tracepanel.core.context import current_trace
from tracepanel.core.frames import awaited_from, origin
from tracepanel.core.patching import patch, patched


def install() -> None:
    if patched(SyncToAsync, "__call__"):
        return
    original: Any = SyncToAsync.__call__

    @functools.wraps(original)
    async def call(self: SyncToAsync[Any, Any], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 (any callable)
        if current_trace() is None:
            return await original(self, *args, **kwargs)
        token = awaited_from.set(origin(skip=1))
        followed = profile.followed(self.func)
        if followed is not self.func:
            # A copy for this call only: the same instance (a view Django adapted once) serves concurrent requests.
            self = copy.copy(self)
            self.func = followed
        try:
            return await original(self, *args, **kwargs)
        finally:
            awaited_from.reset(token)

    patch(SyncToAsync, "__call__", call)
