"""Monkeypatches in one place: each attribute is replaced once, its original remembered, and all can be undone.

Integrations patch frameworks and libraries (Django's Atomic, Celery's signals, Falcon's App…). Going through here
makes every patch idempotent (installing twice is a no-op), lets the code ask whether something is patched, and lets
tests put everything back with undo().
"""

from dataclasses import dataclass
from typing import Any

MISSING = object()


@dataclass(frozen=True, slots=True)
class Patch:
    owner: Any
    name: str
    # What the owner itself had (MISSING: the attribute came from a base class).
    original: Any


_applied: dict[tuple[int, str], Patch] = {}


def patched(owner: object, name: str) -> bool:
    return (id(owner), name) in _applied


def patch(owner: object, name: str, replacement: object) -> bool:
    """Set owner.name to replacement unless tracepanel already patched it; True when it did now."""
    key = (id(owner), name)
    if key in _applied:
        return False
    own = vars(owner).get(name, MISSING) if hasattr(owner, "__dict__") else getattr(owner, name, MISSING)
    _applied[key] = Patch(owner, name, own)
    setattr(owner, name, replacement)
    return True


def original(owner: object, name: str) -> Any:  # noqa: ANN401 (whatever was patched)
    """What owner.name was before tracepanel patched it (the inherited one when the owner had none of its own)."""
    found = _applied.get((id(owner), name))
    if found is None:
        return getattr(owner, name)
    if found.original is MISSING:
        # Inherited: the first base that has it.
        return next(vars(base)[name] for base in type.mro(owner)[1:] if name in vars(base))  # type: ignore[arg-type]
    return found.original


def applied() -> list[Patch]:
    """Every patch in place, in the order they were made."""
    return list(_applied.values())


def undo() -> None:
    """Put every patched attribute back, newest first (for tests)."""
    for patch_ in reversed(list(_applied.values())):
        if patch_.original is MISSING:
            delattr(patch_.owner, patch_.name)
        else:
            setattr(patch_.owner, patch_.name, patch_.original)
    _applied.clear()
