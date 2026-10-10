"""Backports for the oldest supported Python (3.11)."""

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

F = TypeVar("F", bound=Callable[..., object])

if TYPE_CHECKING:
    from typing_extensions import override
else:

    def override(method: F) -> F:
        """No-op at runtime; typing_extensions.override for type checkers."""
        return method


__all__ = ["override"]
