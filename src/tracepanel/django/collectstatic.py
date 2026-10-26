"""collectstatic, where its time goes: a span per phase, the slowest files in each, and the counts on the trace.

`manage.py collectstatic` is a `command` trace (tracepanel.django.commands); a span for each of thousands of files
would bury it. Under the command, a span per phase:

- `collectstatic clear` (`--clear`): deleting what the destination holds;
- `collectstatic copy`: the finders listing the source files and each file's copy (or link), with the checks before
  it (does the destination have it, is it older); `collectstatic.find_ms` is the time outside the files', the listing;
- `collectstatic post-process`: the storage's post_process(), ManifestStaticFilesStorage hashing each file, rewriting
  the URLs in its CSS and JS, and saving the manifest at the end.

Each phase keeps its SLOWEST files as spans (`copy css/app.css`, `post-process css/app.css`), their storage calls
inside, and the SLOWEST storage calls made outside a file; the others are counted on the phase
(`collectstatic.files`, `collectstatic.files_ms`, `collectstatic.storage_calls`). The trace says how many files were
copied, linked, unmodified and post-processed.
"""

import functools
from collections.abc import Callable, Generator, Iterable
from contextvars import ContextVar
from typing import Any

from django.contrib.staticfiles.management.commands import collectstatic
from django.contrib.staticfiles.management.commands.collectstatic import Command

from tracepanel.core.context import OpenSpan, current_trace, open_span
from tracepanel.core.patching import patch, patched
from tracepanel.django import storages

# Files (and storage calls) each phase keeps as spans.
SLOWEST = 10


def _ms(us: int) -> float:
    return round(us / 1000, 1)


class _Phase:
    def __init__(self, name: str) -> None:
        self.name = name
        self.files = storages.Slowest(SLOWEST)
        self.calls = storages.Slowest(SLOWEST, "storage")
        self.span: OpenSpan | None = open_span("function", f"collectstatic {name}", {"collectstatic.phase": name})
        self._outer = storages.pool.get()
        storages.pool.set(self.calls)

    def end(self) -> None:
        storages.pool.set(self._outer)
        if self.span is None:
            return
        self.span.end()
        span = self.span.span
        span.attrs.update(
            {
                "collectstatic.files": self.files.count,
                "collectstatic.files_ms": _ms(self.files.total_us),
                "collectstatic.storage_calls": self.calls.count,
            }
        )
        if self.name == "copy":
            span.attrs["collectstatic.find_ms"] = _ms(max(span.duration - self.files.total_us, 0))


class _Run:
    """One collect(): the phase it is in."""

    def __init__(self) -> None:
        self.phase: _Phase | None = None

    def begin(self, name: str) -> _Phase:
        self.end()
        self.phase = _Phase(name)
        return self.phase

    def end(self) -> None:
        if self.phase is not None:
            self.phase.end()
            self.phase = None

    def file(self, name: str, attrs: dict[str, Any]) -> Any:  # noqa: ANN401 (a context manager, or None)
        """A file's block in the current phase; None outside one."""
        return self.phase.files.block(name, attrs) if self.phase is not None else None


_run: ContextVar[_Run | None] = ContextVar("tracepanel_collectstatic", default=None)


def _counts(command: Command) -> dict[str, int]:
    return {
        "collectstatic.copied": len(command.copied_files),
        "collectstatic.linked": len(command.symlinked_files),
        "collectstatic.unmodified": len(command.unmodified_files),
        "collectstatic.post_processed": len(command.post_processed_files),
    }


def _result(command: Command, before: dict[str, int]) -> str:
    """What happened to the file: copied, linked, unmodified, or skipped (another finder's file had its path)."""
    after = _counts(command)
    for key, result in (
        ("collectstatic.copied", "copied"),
        ("collectstatic.linked", "linked"),
        ("collectstatic.unmodified", "unmodified"),
    ):
        if after[key] > before[key]:
            return result
    return "skipped"


def _file_handler(verb: str, original: Callable[..., object]) -> Callable[..., object]:
    @functools.wraps(original)
    def handler(self: Command, path: str, prefixed_path: str, source_storage: object) -> object:
        run = _run.get()
        block = run.file(f"{verb} {prefixed_path}", {"collectstatic.file": prefixed_path}) if run else None
        if block is None:
            return original(self, path, prefixed_path, source_storage)
        before = _counts(self)
        token = storages.pool.set(None)
        try:
            with block as span:
                result = original(self, path, prefixed_path, source_storage)
                if span is not None:
                    span.attrs["collectstatic.result"] = _result(self, before)
                return result
        finally:
            storages.pool.reset(token)

    return handler


def _post_process(run: _Run, processor: Callable[..., Iterable[tuple[str, str, object]]]) -> Callable[..., object]:
    """The storage's post_process() as a phase: each file it yields is a block, the time until it yields it."""

    @functools.wraps(processor)
    def post_process(*args: object, **kwargs: object) -> Generator[tuple[str, str, object], None, None]:
        phase = run.begin("post-process")
        steps = iter(processor(*args, **kwargs))
        try:
            while True:
                token = storages.pool.set(None)
                try:
                    with phase.files.block("post-process", {}) as span:
                        step = next(steps, None)
                        if span is not None:
                            _named(span, step)
                finally:
                    storages.pool.reset(token)
                if step is None:
                    return
                yield step
        finally:
            run.end()

    return post_process


def _named(span: Any, step: tuple[str, str, object] | None) -> None:  # noqa: ANN401 (a Span)
    if step is None:
        # After the last file: ManifestStaticFilesStorage saves its manifest.
        span.name = "post-process finish"
        return
    original, processed_path, processed = step
    span.name = f"post-process {original}"
    span.attrs.update(
        {
            "collectstatic.file": original,
            "collectstatic.processed_as": processed_path,
            "collectstatic.processed": processed is True,
        }
    )
    if isinstance(processed, Exception):
        span.status = "error"


def _own_post_process(command: Command, run: _Run) -> object | None:
    """Put the phase's post_process() on the storage the command will call it on; the storage, to take it off after."""
    processor = getattr(command.storage, "post_process", None) if command.post_process else None
    owner = getattr(processor, "__self__", None)
    if processor is None or owner is None:
        return None
    try:
        vars(owner)["post_process"] = _post_process(run, processor)
    except TypeError:
        # No __dict__ to put it in (__slots__): the phase stays without its files.
        return None
    return owner


def install() -> None:
    if patched(Command, "collect"):
        return
    collect = Command.collect
    clear_dir = Command.clear_dir
    # The name collect() calls, imported into the command's module (the stubs leave it out).
    get_finders = vars(collectstatic)["get_finders"]

    @functools.wraps(collect)
    def traced_collect(self: Command) -> dict[str, list[str]]:
        trace = current_trace()
        if trace is None:
            return collect(self)
        run = _Run()
        token = _run.set(run)
        owner = _own_post_process(self, run)
        try:
            collected = collect(self)
            trace.attrs.update(_counts(self))
            return collected
        finally:
            run.end()
            _run.reset(token)
            if owner is not None:
                vars(owner).pop("post_process", None)

    @functools.wraps(clear_dir)
    def traced_clear_dir(self: Command, path: str) -> None:
        run = _run.get()
        if run is None or (run.phase is not None and run.phase.name == "clear"):
            return clear_dir(self, path)
        run.begin("clear")
        try:
            return clear_dir(self, path)
        finally:
            run.end()

    @functools.wraps(get_finders)
    def traced_get_finders() -> object:
        # collect() lists the files right after: its copy phase begins.
        run = _run.get()
        if run is not None:
            run.begin("copy")
        return get_finders()

    patch(Command, "collect", traced_collect)
    patch(Command, "clear_dir", traced_clear_dir)
    patch(Command, "copy_file", _file_handler("copy", Command.copy_file))
    patch(Command, "link_file", _file_handler("link", Command.link_file))
    patch(collectstatic, "get_finders", traced_get_finders)
