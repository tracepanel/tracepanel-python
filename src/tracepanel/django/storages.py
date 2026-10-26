"""File storage calls: each call a Django Storage makes to its backend is a span, `storage save avatars/1.png`.

Every Storage class is instrumented, Django's (FileSystemStorage, StaticFilesStorage, InMemoryStorage) and those of
django-storages (S3, Google Cloud, Azure…) or the project's own, whether it is configured in STORAGES or given to a
FileField: the methods each class defines are wrapped where it defines them, now for the classes imported already and
as the others are created. A call made inside another storage call is part of it, not a span of its own: save() asking
exists() for a free name, a subclass's exists() calling super(). What the backend does meanwhile (an S3 request, its
HTTP call) lands inside the span. url() is left out where it only joins strings: on the local file system and in
memory. On S3 it signs the URL.

The agent has no span kind for storage yet: these are `function` spans, their attributes under `storage.`.
"""

import functools
import heapq
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar

from django.core.files.storage import FileSystemStorage, InMemoryStorage, Storage, storages

from tracepanel.core.context import blocking_attrs, current_trace, start_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs, Span, Trace
from tracepanel.core.patching import patch, patched
from tracepanel.django import services

OPERATIONS = (
    "open",
    "save",
    "delete",
    "exists",
    "listdir",
    "size",
    "url",
    "get_accessed_time",
    "get_created_time",
    "get_modified_time",
)
MARKER = "_tracepanel"

# A storage call is running: the calls it makes are part of it.
_inside: ContextVar[bool] = ContextVar("tracepanel_storage_call", default=False)


class Slowest:
    """Many calls of one kind (collectstatic's thousands of files): the `keep` slowest stay spans, the rest are counted.

    Each block is a span while it runs. Once it ends, it stays only if it is among the slowest so far (or failed): the
    spans of the one it displaces, and those of a faster one, are dropped with everything recorded inside them. Logs
    and errors bound to a dropped span move to its parent.
    """

    def __init__(self, keep: int) -> None:
        self.keep = keep
        self.count = 0
        self.total_us = 0
        self._kept: list[tuple[int, int, Span]] = []

    @contextmanager
    def block(self, name: str, attrs: Attrs) -> Generator[Span | None, None, None]:
        trace = current_trace()
        mark = len(trace.spans) if trace is not None else 0
        with start_span("function", name, attrs) as span:
            yield span
        if span is None or trace is None:
            return
        self.count += 1
        self.total_us += span.duration
        if span.status == "error":
            return
        entry = (span.duration, self.count, span)
        if len(self._kept) < self.keep:
            heapq.heappush(self._kept, entry)
        elif span.duration > self._kept[0][0]:
            _drop(trace, heapq.heapreplace(self._kept, entry)[2])
        else:
            _drop(trace, span, mark)


def _drop(trace: Trace, root: Span, start: int = 0) -> None:
    """Take `root` and the spans under it out of the trace (they come after it: a span is added as it starts)."""
    gone = {root.id}
    for span in trace.spans[start:]:
        if span.parent_id in gone:
            gone.add(span.id)
    trace.spans[start:] = [span for span in trace.spans[start:] if span.id not in gone]
    for item in (*trace.logs, *trace.errors):
        if item.span_id in gone:
            item.span_id = root.parent_id


# collectstatic's phase, while one runs: its storage calls are blocks of it, the slowest kept (None: each is a span).
pool: ContextVar[Slowest | None] = ContextVar("tracepanel_storage_pool", default=None)


def alias_of(storage: Storage) -> str | None:
    """The storage's name in STORAGES (`default`, `staticfiles`); None for one made elsewhere (a FileField's own)."""
    created = getattr(storages, "_storages", None) or {}
    return next((alias for alias, made in created.items() if made is storage), None)


def _local(storage: Storage) -> bool:
    return isinstance(storage, (FileSystemStorage, InMemoryStorage))


def _size(file: object) -> int | None:
    """A File's size when it knows it without reading: an upload's, a ContentFile's, a local file's."""
    try:
        size = getattr(file, "size", None)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return size if isinstance(size, int) else None


def _name(op: str, path: object) -> str:
    return f"storage {op} {path or '.'}"


def _before(op: str, args: tuple[object, ...], kwargs: dict[str, object]) -> Attrs:
    if op != "save":
        return {}
    content = args[1] if len(args) > 1 else kwargs.get("content")
    size = _size(content)
    return {} if size is None else {"storage.bytes": size}


def _after(op: str, storage: Storage, path: object, result: object) -> Attrs:
    """What the call answered: whether a file exists, how big it is, what a listing holds, the name a save chose."""
    if op == "exists":
        return {"storage.exists": bool(result)}
    if op == "size" and isinstance(result, int):
        return {"storage.bytes": result}
    if op == "listdir" and isinstance(result, tuple) and len(result) == 2:  # noqa: PLR2004 (directories, files)
        dirs, files = result
        return {"storage.dirs": len(dirs), "storage.files": len(files)}
    if op == "save" and result != path:
        return {"storage.saved_as": str(result)}
    # Reading a remote file's size would be another request: only a local one's.
    size = _size(result) if op == "open" and _local(storage) else None
    return {} if size is None else {"storage.bytes": size}


def _wrap(op: str, method: Callable[..., object]) -> Callable[..., object]:
    @functools.wraps(method)
    def wrapped(self: Storage, *args: object, **kwargs: object) -> object:
        if _inside.get() or current_trace() is None or (op == "url" and _local(self)):
            return method(self, *args, **kwargs)
        path = args[0] if args else kwargs.get("name", kwargs.get("path"))
        kind = type(self)
        attrs: Attrs = {
            "storage.alias": alias_of(self),
            "storage.backend": f"{kind.__module__}.{kind.__qualname__}",
            "storage.system": services.storage_system(self),
            "storage.op": op,
            "storage.path": None if path is None else str(path),
            **_before(op, args, kwargs),
            "code.origin": origin(),
            **blocking_attrs(),
        }
        phase = pool.get()
        block: AbstractContextManager[Span | None] = (
            phase.block(_name(op, path), attrs) if phase is not None else start_span("function", _name(op, path), attrs)
        )
        token = _inside.set(True)
        try:
            # Open while the backend works: its own calls (an S3 request) land inside.
            with block as span:
                result = method(self, *args, **kwargs)
                if span is not None:
                    span.attrs.update(_after(op, self, path, result))
                return result
        finally:
            _inside.reset(token)

    setattr(wrapped, MARKER, True)
    return wrapped


def instrument(kind: type[Storage]) -> None:
    """Wrap the operations the class defines, and those it takes from a mixin that isn't a Storage (HashedFilesMixin).

    Those it inherits from another Storage are wrapped there.
    """
    for op in OPERATIONS:
        owner = next((base for base in kind.__mro__ if op in vars(base)), None)
        if owner is None or (owner is not kind and issubclass(owner, Storage)):
            continue
        method = vars(owner)[op]
        if callable(method) and not getattr(method, MARKER, False):
            patch(kind, op, _wrap(op, method))


def _subclasses(kind: type[Storage]) -> list[type[Storage]]:
    found: list[type[Storage]] = []
    for sub in kind.__subclasses__():
        found += [sub, *_subclasses(sub)]
    return found


def _init_subclass(cls: type[Storage], **kwargs: object) -> None:
    super(Storage, cls).__init_subclass__(**kwargs)
    instrument(cls)


def install() -> None:
    if patched(Storage, "__init_subclass__"):
        return
    for kind in (Storage, *_subclasses(Storage)):
        instrument(kind)
    # Storage classes created from now on (django-storages' backends, imported once STORAGES is first used).
    patch(Storage, "__init_subclass__", classmethod(_init_subclass))
