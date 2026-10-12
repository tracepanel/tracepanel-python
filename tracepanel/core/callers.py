"""Who called a library tracepanel wraps: the app, or a layer that records the call itself.

A database driver, an HTTP client and Redis are wrapped where the app calls them, and also called by what tracepanel
already records: Django's ORM and cache, SQLAlchemy, the task queues, tracepanel itself. The stack tells them apart:
the first frame above the call that is neither the wrapper's own nor the library's. That is the caller, and the call
is the app's to record unless the caller is one of those layers. The nearest caller decides, not anything up the
stack: the middleware that wraps the whole request is there too.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from types import FrameType

# A bound on the frames passed over; the caller is a few frames up.
_DEPTH = 32


@dataclass(frozen=True)
class Callers:
    """How to read the stack above one wrapped library.

    own: the wrapper's module (or package), whose frames sit between the library and its caller; met again further
    up, it means a wrapper further out records this call already (a library calling its own wrapped method).
    libraries: the wrapped library and those built on it (requests on urllib3), passed over to find the caller.
    layers: the callers that record the call themselves, or make it for their own ends (a pool's housekeeping).
    housekeeping: (module, function) pairs inside the libraries whose calls are theirs, not the app's.
    """

    own: str
    libraries: tuple[str, ...]
    layers: tuple[str, ...] = ("tracepanel.",)
    housekeeping: Collection[tuple[str, str]] = field(default_factory=frozenset)

    def app_called(self, frame: FrameType | None) -> bool:
        """Whether the call made from this frame (the wrapper's) is the app's to record."""
        while frame is not None and frame.f_globals.get("__name__", "").startswith(self.own):
            frame = frame.f_back
        for _ in range(_DEPTH):
            if frame is None:
                return True
            name: str = frame.f_globals.get("__name__", "")
            if name.startswith((self.own, *self.layers)):
                return False
            if not name.startswith(self.libraries):
                return True
            if (name, frame.f_code.co_name) in self.housekeeping:
                return False
            frame = frame.f_back
        return True
