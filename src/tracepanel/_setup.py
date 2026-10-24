"""The steps every web framework's install() takes; Django's AppConfig takes the same ones.

    if not _setup.enable(settings):   # the framework's settings, then: is tracepanel on?
        return
    ...                               # the framework's own part: its middleware, its patches
    _setup.started("Flask")           # the integrations, the console's EXPLAIN, the terminal lines

Each framework module keeps only what is specific to it.
"""

from collections.abc import Callable, Mapping

from tracepanel.core import conf


def enable(source: Callable[[], Mapping[str, object]]) -> bool:
    """The framework's settings (read on each load: debug mode can change at startup); whether tracepanel is on."""
    conf.use_source(source)
    return conf.get_config().enabled


def started(framework: str, *, debug: str = "debug mode", django: bool = False, announce: bool = True) -> None:
    """The framework is set up: install the integrations, register EXPLAIN, and say so in the terminal."""
    from tracepanel.console import app as app_console  # noqa: PLC0415 (only once an app installs tracepanel)
    from tracepanel.console import terminal  # noqa: PLC0415
    from tracepanel.core import accesslog, registry, runtime  # noqa: PLC0415
    from tracepanel.integrations import dbapi, sqlalchemy  # noqa: PLC0415

    runtime.set_framework(framework)
    accesslog.install()
    registry.install(django=django)
    # EXPLAIN in the console runs where a query came from: a driver's database, else the SQLAlchemy engine (Django's own
    # runs on its connection).
    if not django:
        app_console.register("explain", app_console.explain_handler(dbapi.explain_or(sqlalchemy.explain)))
    terminal.install()
    if announce:
        terminal.announce(framework, debug=debug)
