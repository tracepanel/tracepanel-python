"""Locks a forked process starts with free.

A worker that forks copies only the thread that forks: RQ's work horse for each job, Celery's prefork pool, gunicorn
with `--preload`. A lock another thread held at that moment (the writer sending to the agent, a heartbeat listing the
tasks it runs) stays held in the child, and its first use there waits forever. Each lock of this package is made free
again in the child; what it guards is as the forking thread left it.
"""

import os
import threading
from collections.abc import Callable


def lock() -> threading.Lock:
    """A lock, free again in a forked child."""
    made = threading.Lock()
    # What CPython's own modules (logging, concurrent.futures) use for theirs; on every supported Python.
    reinit = getattr(made, "_at_fork_reinit", None)
    if reinit is not None:
        after_fork(reinit)
    return made


def after_fork(reset: Callable[[], object]) -> None:
    """Run `reset` in each forked child, as it starts; nothing where processes don't fork (Windows)."""
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=reset)
