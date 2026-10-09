"""Event model matching schema/trace.schema.json."""

from dataclasses import dataclass, field

Scalar = str | int | float | bool | None
AttrValue = Scalar | list[Scalar]
Attrs = dict[str, AttrValue]


@dataclass(slots=True)
class Span:
    id: str
    parent_id: str | None
    kind: str
    name: str
    start: int
    duration: int = 0
    status: str = "ok"
    attrs: Attrs = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        # By hand: a trace has many spans, and attrs hold plain values already.
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "start": self.start,
            "duration": self.duration,
            "status": self.status,
            "attrs": self.attrs,
        }


@dataclass(slots=True)
class Log:
    span_id: str | None
    time: int
    level: str
    logger: str
    message: str
    exception: str | None = None
    # code.*, thread.*, process.*, log.template and extra.* (the record's `extra=` fields).
    attrs: Attrs = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "span_id": self.span_id,
            "time": self.time,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "exception": self.exception,
            "attrs": self.attrs,
        }


@dataclass(slots=True)
class Frame:
    filename: str
    module: str
    function: str
    lineno: int
    in_app: bool
    # Source lines around `lineno`; `context_start` is the line number of the first one.
    context_start: int | None = None
    context: list[str] = field(default_factory=list)
    # repr() of local variables, project frames only; sensitive names are masked.
    locals: dict[str, str] | None = None


@dataclass(slots=True)
class ChainedError:
    """An exception linked to the reported one: `cause` (raise ... from) or `context` (raised while handling)."""

    relation: str
    type: str
    message: str
    frames: list[Frame]


@dataclass(slots=True)
class Error:
    span_id: str | None
    time: int
    type: str
    message: str
    handled: bool
    frames: list[Frame]
    # Inner exceptions, nearest first.
    chain: list[ChainedError] = field(default_factory=list)


@dataclass(slots=True)
class Link:
    kind: str
    trace_id: str
    span_id: str


@dataclass(slots=True)
class Issue:
    fingerprint: str
    fingerprint_version: int
    kind: str
    title: str
    culprit: str | None
    hint: str | None
    count: int
    span_ids: list[str]


@dataclass(slots=True)
class Trace:
    id: str
    kind: str
    name: str
    start: int
    duration: int = 0
    status: str = "ok"
    attrs: Attrs = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)
    logs: list[Log] = field(default_factory=list)
    errors: list[Error] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """The trace as JSON-ready dicts, without copying values (it is serialized right away, never mutated)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "start": self.start,
            "duration": self.duration,
            "status": self.status,
            "attrs": self.attrs,
            "spans": [span.to_dict() for span in self.spans],
            "logs": [log.to_dict() for log in self.logs],
            # Few and nested (frames, chains): the generic way.
            "errors": _plain(self.errors),
            "links": _plain(self.links),
            "issues": _plain(self.issues),
        }


def _plain(value: object) -> object:
    """Dataclasses to dicts, recursively, without copying the values (dataclasses.asdict deep-copies everything)."""
    if isinstance(value, list):
        return [_plain(item) for item in value]
    fields = getattr(type(value), "__dataclass_fields__", None)
    if fields is None:
        return value
    return {name: _plain(getattr(value, name)) for name in fields}
