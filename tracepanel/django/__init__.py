"""Django, the framework tracepanel is built for: `INSTALLED_APPS += ["tracepanel.django"]`.

Everything that knows Django lives here, on top of the framework-neutral core (model, context, analysis), storage
and console.

install() runs from the AppConfig: it gives the core Django's settings (TRACEPANEL, DEBUG as `enabled`, BASE_DIR as
`root`), puts the middleware first and installs the collectors. It installs nothing unless tracepanel is enabled: by
DEBUG, unless TRACEPANEL_ENABLED (set by `tracepanel run`) or TRACEPANEL["enabled"] says otherwise, as for every
framework. User switching needs DEBUG itself (tracepanel.django.users).
"""

import sys
from collections.abc import Mapping
from pathlib import Path

from django.conf import settings
from django.core.signals import setting_changed

from tracepanel.core import conf

APP = "tracepanel.django"
MIDDLEWARE = "tracepanel.django.middleware.TracepanelMiddleware"
# Settings that change tracepanel's configuration (override_settings, pytest-django's `settings`).
WATCHED = frozenset({"TRACEPANEL", "DEBUG", "BASE_DIR", "INSTALLED_APPS"})


def settings_source() -> Mapping[str, object]:
    """Django's settings for the core: DEBUG turns tracepanel on, BASE_DIR is the project; TRACEPANEL wins over both."""
    root = getattr(settings, "BASE_DIR", None)
    return {
        "enabled": bool(settings.DEBUG) and APP in settings.INSTALLED_APPS,
        **({"root": str(root)} if root else {}),
        **getattr(settings, "TRACEPANEL", {}),
    }


def on_setting_changed(setting: str = "", **kwargs: object) -> None:  # noqa: ARG001 (signal API)
    if setting in WATCHED:
        conf.reset()


# Management commands that serve requests: the others get their own line when they finish (the terminal summary).
SERVERS = frozenset({"runserver", "runserver_plus", "runworker"})


def _serving() -> bool:
    """Not a management command other than a server: manage.py runserver, or a WSGI/ASGI server importing the app."""
    program = Path(sys.argv[0]).name if sys.argv else ""
    if program in {"manage.py", "django-admin"}:
        return bool(set(sys.argv[1:2]) & SERVERS)
    return True


def install() -> None:
    from tracepanel import _setup  # noqa: PLC0415
    from tracepanel.django import console  # noqa: PLC0415

    setting_changed.connect(on_setting_changed, dispatch_uid="tracepanel.settings")
    # A development tool: nothing is installed unless enabled (DEBUG, or TRACEPANEL_ENABLED / `tracepanel run`).
    if not _setup.enable(settings_source):
        return
    console.install()
    from tracepanel.django import (  # noqa: PLC0415 (collectors patch Django: import them only when used)
        auth,
        cache,
        commands,
        errors,
        mail,
        signals,
        sql,
        structure,
        sync_to_async,
        transactions,
    )

    # INSTALLED_APPS is the only setup step: the middleware goes first, so it measures the whole request.
    if MIDDLEWARE not in settings.MIDDLEWARE:
        settings.MIDDLEWARE = [MIDDLEWARE, *settings.MIDDLEWARE]
    sync_to_async.install()
    sql.install()
    transactions.install()
    cache.install()
    auth.install()
    errors.install()
    structure.install()
    mail.install()
    signals.install()
    commands.install()
    # Libraries and Django apps (DRF, Ninja, Channels, Django Tasks): each on when installed and not turned off.
    _setup.started("Django", debug="DEBUG", django=True, announce=_serving())
