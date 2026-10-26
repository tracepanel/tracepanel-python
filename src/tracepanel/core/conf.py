"""Configuration, whatever runs tracepanel: each layer overrides the one before it.

1. defaults (Config below);
2. the project's file: `tracepanel.toml`, or `[tool.tracepanel]` in `pyproject.toml`, found from the project
   root upward;
3. the framework's settings (Django: settings.TRACEPANEL, DEBUG as `enabled`, BASE_DIR as `root`), via use_source();
4. configure() in code;
5. TRACEPANEL_<NAME> variables (`tracepanel run --set name=value` sets them).

Every value is checked against its field's type, with the layer named in the error. explain() tells where each
value came from (`tracepanel config`). The result is cached (collectors read it on every SQL query): reset() reloads.
"""

import fnmatch
import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TypedDict, Unpack

from tracepanel.core import forks

TRUE = ("1", "true", "yes", "on")
FALSE = ("0", "false", "no", "off")


@dataclass(frozen=True, slots=True)
class Config:
    # Whether tracepanel records anything: the framework integration decides (Django: DEBUG).
    enabled: bool = False
    # The project's directory: stack paths are relative to it, the store lives in it, it names the service.
    root: str = ""
    # This project's name on the service map; the project directory's name when empty.
    service: str = ""
    # Inject widget.min.js into HTML responses.
    widget: bool = True
    # URL prefix for the UI, widget.min.js and the JSON API.
    prefix: str = "/__tracepanel__/"
    # Leave the requests under `prefix` (the widget's polling, the console) out of the server's access log: uvicorn,
    # gunicorn, hypercorn, granian, daphne, Django's and Flask's development servers.
    quiet_access_log: bool = True
    # The project's store directory (the agent's), relative to `root`.
    dir: str = ".tracepanel"
    # Where traces go: "auto" to the project's agent (started on demand), the address of one elsewhere
    # (`tcp:host.docker.internal:7701`, its console at `console_url`), or "off": traces aren't kept.
    agent: str = "auto"
    # The console of an agent named in `agent` (elsewhere), and where terminal lines link without an agent of ours.
    console_url: str = "http://127.0.0.1:8765/"
    # A line in the terminal after each task run and management command, with a link to its trace.
    terminal_summary: bool = True
    # Traces the agent keeps; older ones are deleted.
    max_traces: int = 2000
    # Similar queries with different parameters reported as N+1.
    n_plus_one_threshold: int = 3
    # Repeated Redis commands or cache calls reported only when they cost this much together (in-memory is cheap).
    n_plus_one_min_ms: int = 5
    # Queries slower than this are reported as slow SQL.
    slow_sql_ms: int = 100
    # Outgoing HTTP calls slower than this are reported as slow.
    slow_http_ms: int = 500
    # Outer transactions (atomic blocks) longer than this are reported: they hold their locks until COMMIT.
    long_transaction_ms: int = 500
    # Keep repr() of local variables in project frames of errors (development only, masked).
    capture_locals: bool = True
    # Keep the text and HTML of sent emails (development only; they never leave this machine).
    capture_mail_body: bool = True
    # Keep SQL parameter values so the console can run EXPLAIN (development only; they never leave this machine).
    capture_sql_params: bool = True
    # Let the widget log in as another user (development only, from this machine).
    user_switch: bool = True
    # Keep repr() of Celery task arguments and results (development only, masked; they never leave this machine).
    capture_task_args: bool = True
    # Keep a request's headers, query, body (what the app read), cookies and session (development only, masked; they
    # never leave this machine).
    capture_request: bool = True
    # Keep the bodies of requests and of their responses with it: JSON and forms (masked) and text, a file upload by
    # its name and size, never a stream; a response's pages, scripts and binary content by their size only.
    capture_body: bool = True
    # How much of a body is kept, in KiB: a longer one is cut there, marked as cut.
    max_body_kb: int = 64
    # Send the app's settings (Django's, Flask's app.config) to the console, masked (development only; they never
    # leave this machine).
    capture_settings: bool = True
    # Keep repr() of the context each template is rendered with (development only, masked; it never leaves this
    # machine).
    capture_template_context: bool = True
    # Sample where each trace's time goes in code (a flame graph in the console): every trace when on; one request
    # with an X-Tracepanel-Profile: 1 header or a tracepanel_profile=1 cookie either way.
    profile: bool = False
    # Count every profiled trace's memory too (tracemalloc), not only a request's that asks with "memory" in the
    # header or cookie. It slows down the whole process while a trace is counted, a short request several times over.
    profile_memory: bool = False
    # Whether profiling is possible at all: off, neither the setting nor a request's header or cookie profiles.
    profile_enabled: bool = True
    # How often the profiler samples a profiled trace's stack: each millisecond, so a request of a few milliseconds
    # still has a profile to read. Only a profiled trace pays for it.
    profile_interval_ms: int = 1
    # Integrations, each on when its library is installed; `false` leaves the library unpatched. Read at startup.
    # AWS calls through botocore (boto3, aiobotocore): S3, DynamoDB, SQS, SNS and the other services.
    boto_enabled: bool = True
    celery_enabled: bool = True
    channels_enabled: bool = True
    # Database drivers used directly, without an ORM (statements an ORM runs are the ORM's).
    dbapi_enabled: bool = True
    django_q_enabled: bool = True
    django_tasks_enabled: bool = True
    dramatiq_enabled: bool = True
    drf_enabled: bool = True
    faststream_enabled: bool = True
    # graphene-django's GraphQLView.
    graphene_enabled: bool = True
    # Outgoing HTTP calls: urllib3 (requests), httpx, aiohttp. boto3's are AWS calls (boto_enabled).
    http_clients_enabled: bool = True
    huey_enabled: bool = True
    jinja_enabled: bool = True
    logging_enabled: bool = True
    # Django's migrate: a span per migration applied or unapplied and per operation, risky operations marked. Read at
    # startup.
    migrations_enabled: bool = True
    mongodb_enabled: bool = True
    ninja_enabled: bool = True
    otel_enabled: bool = True
    # Redis used directly (Django's cache and the task queues record their own calls).
    redis_enabled: bool = True
    rq_enabled: bool = True
    # Django signals: each send with receivers is a span, each receiver a span inside it.
    signals_enabled: bool = True
    sqlalchemy_enabled: bool = True
    # Django's file storages: each call that reaches the backend is a span, and collectstatic's phases. Read at startup.
    storage_enabled: bool = True
    strawberry_enabled: bool = True
    taskiq_enabled: bool = True
    # Requests not traced at all: glob patterns on the path (health checks, static files).
    ignore_paths: tuple[str, ...] = ("/health*", "/static/*", "/media/*", "/favicon.ico")
    # Tasks not traced: glob patterns on the task name, e.g. "celery.backend_cleanup".
    ignore_tasks: tuple[str, ...] = ()
    # Loggers whose records are left out (with their children), on top of tracepanel's own and SQL echo.
    ignore_loggers: tuple[str, ...] = ()
    # Django signals left out entirely, glob patterns on the name (post_save) or the dotted path
    # (shop.signals.order_placed). pre_init and post_init fire for every object a query loads, class_prepared for every
    # model class built (each historical model a migration renders): by default even their count would cost more than
    # it tells. Read at startup.
    ignore_signals: tuple[str, ...] = ("pre_init", "post_init", "class_prepared")
    # Management commands not traced: long-running and interactive ones by default (servers, shells, workers).
    ignore_commands: tuple[str, ...] = (
        "runserver",
        "runserver_plus",
        "shell",
        "shell_plus",
        "dbshell",
        "test",
        "testserver",
        # Task workers run for days: each task they run is its own trace.
        "db_worker",
        "rqworker",
        "rqscheduler",
        "rundramatiq",
        "run_huey",
        "qcluster",
        "qmonitor",
        "runworker",
    )


class Settings(TypedDict, total=False):
    """Config's fields as keyword arguments (`install(app, slow_sql_ms=50)`): see Config for what each one does."""

    enabled: bool
    root: str
    service: str
    widget: bool
    prefix: str
    quiet_access_log: bool
    dir: str
    agent: str
    console_url: str
    terminal_summary: bool
    max_traces: int
    n_plus_one_threshold: int
    n_plus_one_min_ms: int
    slow_sql_ms: int
    slow_http_ms: int
    long_transaction_ms: int
    capture_locals: bool
    capture_mail_body: bool
    capture_sql_params: bool
    user_switch: bool
    capture_task_args: bool
    capture_request: bool
    capture_body: bool
    max_body_kb: int
    capture_settings: bool
    capture_template_context: bool
    profile: bool
    profile_memory: bool
    profile_enabled: bool
    profile_interval_ms: int
    boto_enabled: bool
    celery_enabled: bool
    channels_enabled: bool
    dbapi_enabled: bool
    django_q_enabled: bool
    django_tasks_enabled: bool
    dramatiq_enabled: bool
    drf_enabled: bool
    faststream_enabled: bool
    graphene_enabled: bool
    http_clients_enabled: bool
    huey_enabled: bool
    jinja_enabled: bool
    logging_enabled: bool
    migrations_enabled: bool
    mongodb_enabled: bool
    ninja_enabled: bool
    otel_enabled: bool
    redis_enabled: bool
    rq_enabled: bool
    signals_enabled: bool
    sqlalchemy_enabled: bool
    storage_enabled: bool
    strawberry_enabled: bool
    taskiq_enabled: bool
    ignore_paths: list[str] | tuple[str, ...]
    ignore_tasks: list[str] | tuple[str, ...]
    ignore_loggers: list[str] | tuple[str, ...]
    ignore_signals: list[str] | tuple[str, ...]
    ignore_commands: list[str] | tuple[str, ...]


def matches(value: str, patterns: tuple[str, ...]) -> bool:
    """Glob match (`*`, `?`) against any pattern."""
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


_framework: Callable[[], Mapping[str, object]] = dict
_configured: dict[str, object] = {}
_cached: Config | None = None
# Counts reset()s: a load begun before the last one read what was there before it, and is not kept.
_generation = 0
_lock = forks.lock()


def use_source(source: Callable[[], Mapping[str, object]]) -> None:
    """The framework's settings (read on each load): the integration calls this once, at startup."""
    global _framework  # noqa: PLW0603 (one framework per process)
    _framework = source
    reset()


def configure(**values: Unpack[Settings]) -> None:
    """Set values in code; they win over the framework's settings, TRACEPANEL_* variables still win over them."""
    _configured.update(values)
    reset()


def get_config() -> Config:
    """The configuration, loaded once and cached.

    Another thread (a heartbeat, the writer) may load it while use_source() or reset() runs: its load read the layers
    as they were, and would put the configuration from before back in the cache. Only a load no reset() went past is
    kept; the one it returns is its caller's.
    """
    global _cached  # noqa: PLW0603 (a process-wide cache)
    cached = _cached
    if cached is not None:
        return cached
    generation = _generation
    loaded = _load()
    with _lock:
        if generation == _generation and _cached is None:
            _cached = loaded
    return loaded


def reset() -> None:
    """Forget the cached configuration: the next get_config() reads everything again."""
    global _cached, _generation  # noqa: PLW0603
    with _lock:
        _generation += 1
        _cached = None


class ConfigError(ValueError):
    """A setting with a wrong name or type: the message names the layer (file, variable, settings)."""


FILES = ("tracepanel.toml", "pyproject.toml")


def find_file(start: Path) -> tuple[Path, dict[str, object]] | None:
    """The project's tracepanel settings: the nearest tracepanel.toml or pyproject.toml with [tool.tracepanel].

    A pyproject.toml without the section is the project's boundary: the search stops there.
    """
    for directory in (start, *start.parents):
        tracepanel_toml = directory / "tracepanel.toml"
        if tracepanel_toml.is_file():
            return tracepanel_toml, _read_toml(tracepanel_toml)
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            section = _read_toml(pyproject).get("tool", {})
            settings = section.get("tracepanel") if isinstance(section, dict) else None
            return (pyproject, dict(settings)) if isinstance(settings, dict) else None
    return None


def _read_toml(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"{path}: {exc}"
        raise ConfigError(msg) from exc


def _field_kind(name: str) -> object:
    return next(f.type for f in fields(Config) if f.name == name)


def _from_env(name: str, raw: str) -> object:
    """A TRACEPANEL_<NAME> value as its field's type; lists are comma-separated."""
    kind = _field_kind(name)
    where = f"TRACEPANEL_{name.upper()}"
    if kind is bool:
        lowered = raw.strip().lower()
        if lowered in TRUE:
            return True
        if lowered in FALSE:
            return False
        msg = f"{where}: expected one of {', '.join(TRUE + FALSE)}, got {raw!r}"
        raise ConfigError(msg)
    if kind is int:
        try:
            return int(raw)
        except ValueError:
            msg = f"{where}: expected a whole number, got {raw!r}"
            raise ConfigError(msg) from None
    if kind is str:
        return raw
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _checked(name: str, value: object, origin: str) -> object:
    """A value from a file, the framework's settings or configure(): already typed, so only checked."""
    kind = _field_kind(name)
    if kind is int and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind in {bool, str} and isinstance(value, kind):  # type: ignore[arg-type]
        return value
    if kind not in {bool, int, str} and isinstance(value, (list, tuple)) and all(isinstance(i, str) for i in value):
        return tuple(value)
    expected = {bool: "true or false", int: "a whole number", str: "a string"}.get(kind, "a list of strings")  # type: ignore[call-overload]
    msg = f"{origin}: {name} must be {expected}, got {value!r}"
    raise ConfigError(msg)


def _layers() -> list[tuple[str, dict[str, object]]]:
    """(origin, values) from weakest to strongest; values are typed and checked."""
    names = {f.name for f in fields(Config)}
    framework = dict(_framework())
    configured = dict(_configured)
    root = Path(str(configured.get("root") or framework.get("root") or Path.cwd()))
    layers: list[tuple[str, dict[str, object]]] = []
    found = find_file(root)
    if found is not None:
        path, values = found
        origin = str(path.name if path.parent == root else path)
        if path.name == "pyproject.toml":
            origin += " [tool.tracepanel]"
        layers.append((origin, values))
    layers += [("framework settings", framework), ("configure()", configured)]
    checked: list[tuple[str, dict[str, object]]] = []
    for origin, values in layers:
        unknown = values.keys() - names
        if unknown:
            msg = f"{origin}: unknown settings {', '.join(sorted(unknown))}"
            raise ConfigError(msg)
        checked.append((origin, {name: _checked(name, value, origin) for name, value in values.items()}))
    env = {
        name: os.environ[f"TRACEPANEL_{name.upper()}"] for name in names if f"TRACEPANEL_{name.upper()}" in os.environ
    }
    checked += [(f"env TRACEPANEL_{name.upper()}", {name: _from_env(name, raw)}) for name, raw in env.items()]
    return checked


def explain() -> list[tuple[str, object, str]]:
    """(name, value, where it came from) for every setting: what `tracepanel config` prints."""
    origins = {f.name: "default" for f in fields(Config)}
    for origin, values in _layers():
        for name in values:
            origins[name] = origin
    # Unset, these two are derived rather than defaults.
    if origins["root"] == "default":
        origins["root"] = "current directory"
    if origins["service"] == "default":
        origins["service"] = "name of root"
    config = get_config()
    return [(f.name, getattr(config, f.name), origins[f.name]) for f in fields(Config)]


def _load() -> Config:
    values: dict[str, object] = {}
    for _, layer in _layers():
        values.update(layer)
    root = str(values.get("root") or Path.cwd())
    values["root"] = root
    values["service"] = values.get("service") or Path(root).name
    return Config(**values)  # type: ignore[arg-type]  # each value was checked against its field


def project_root() -> Path:
    return Path(get_config().root)
