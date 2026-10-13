"""Django AppConfig: `INSTALLED_APPS += ["tracepanel.django"]` is all a project does."""

from django.apps import AppConfig

from tracepanel._compat import override


class TracepanelConfig(AppConfig):
    name = "tracepanel.django"
    label = "tracepanel"
    verbose_name = "tracepanel"

    @override
    def ready(self) -> None:
        from tracepanel import django  # noqa: PLC0415 (apps must not import models at module level)

        django.install()
