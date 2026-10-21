"""Django's part of the App tab and of a template's context: its settings, its apps' packages, its lazy objects.

A QuerySet shows its model, and its row count once it was evaluated: its repr() would run it. A lazy object (a
translation, reverse_lazy) stays unevaluated. While a value is shown no query runs: a model's __str__ or a Page's
count would make one, and is shown as not shown instead.
"""

import contextlib
from collections.abc import Iterable, Iterator
from importlib import metadata

from django.apps import apps
from django.conf import settings
from django.db import connections
from django.db.models.query import QuerySet
from django.utils.functional import Promise

from tracepanel.core import appinfo, values


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


def _blocked(*_args: object, **_kwargs: object) -> object:
    raise values.Blocked


@contextlib.contextmanager
def no_queries() -> Iterator[None]:
    """No query runs meanwhile on this thread's connections: opening a cursor raises values.Blocked.

    Before the cursor, not in an execute wrapper: a debug cursor logs a statement even when a wrapper refuses it.
    """
    blocked = []
    for connection in connections.all():
        connection.cursor = connection.chunked_cursor = _blocked  # type: ignore[method-assign]
        blocked.append(connection)
    try:
        yield
    finally:
        for connection in blocked:
            for name in ("cursor", "chunked_cursor"):
                with contextlib.suppress(AttributeError):
                    delattr(connection, name)


def install() -> None:
    appinfo.use_settings(_settings)
    appinfo.add_packages(_app_packages)
    values.summarize(summary)
    values.guard(no_queries)
