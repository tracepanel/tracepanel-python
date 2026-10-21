"""Django's part of the App tab and of a template's context: its settings, its apps' packages, its lazy objects.

A QuerySet shows its model, and its row count once it was evaluated: its repr() would run it. A lazy object (a
translation, reverse_lazy) stays unevaluated. While a value is shown no query runs: a model's __str__ or a Page's
count would make one, and is shown as not shown instead.
"""

import contextlib
import functools
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from importlib import metadata

from django.apps import apps
from django.conf import settings
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models.query import QuerySet
from django.utils.functional import Promise

from tracepanel.core import appinfo, values
from tracepanel.core.patching import patch, patched


def _settings() -> dict[str, object]:
    found: dict[str, object] = {}
    for name in dir(settings):
        if name.isupper():
            with contextlib.suppress(Exception):
                found[name] = getattr(settings, name)
    return found


def _app_packages() -> Iterable[str]:
    """The distributions the INSTALLED_APPS come from: django-extensions, djangorestframework, the project's none."""
    owners = metadata.packages_distributions()
    tops = {config.module.__name__.split(".", 1)[0] for config in apps.get_app_configs() if config.module}
    return [name for top in sorted(tops) for name in owners.get(top, [])]


def summary(value: object) -> str | None:
    """A QuerySet or a lazy object as shown without evaluating it; None for anything else."""
    if isinstance(value, QuerySet):
        model = value.model.__name__ if value.model is not None else "?"
        cached = value._result_cache  # noqa: SLF001 (whether it ran: nothing public says so)
        if cached is None:
            return f"<QuerySet of {model}, not evaluated>"
        return f"<QuerySet of {model}: {len(cached)} row{'' if len(cached) == 1 else 's'}>"
    if isinstance(value, Promise):
        return "<lazy object, not evaluated>"
    return None


# Set while a value is shown: no cursor opens meanwhile, in this thread or task.
_showing: ContextVar[bool] = ContextVar("tracepanel_showing", default=False)


def no_queries() -> Callable[[], None]:
    """No query runs until the returned call: opening a cursor raises values.Blocked (see _guard_cursors)."""
    token = _showing.set(True)
    return lambda: _showing.reset(token)


def _guard_cursors() -> None:
    """Every cursor, plain or chunked, on every backend, comes from _cursor(): it refuses one while a value is shown.

    Before the cursor, not in an execute wrapper: a debug cursor logs a statement even when a wrapper refuses it.
    """
    if patched(BaseDatabaseWrapper, "_cursor"):
        return
    # The one place every cursor comes from; private, so the stubs don't know it.
    original: Callable[..., object] = getattr(BaseDatabaseWrapper, "_cursor")  # noqa: B009

    @functools.wraps(original)
    def _cursor(self: BaseDatabaseWrapper, *args: object, **kwargs: object) -> object:
        if _showing.get():
            raise values.Blocked
        return original(self, *args, **kwargs)

    patch(BaseDatabaseWrapper, "_cursor", _cursor)


def install() -> None:
    appinfo.use_settings(_settings)
    appinfo.add_packages(_app_packages)
    values.summarize(summary)
    values.guard(no_queries)
    _guard_cursors()
