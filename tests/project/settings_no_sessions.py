"""Auth without sessions: logins are recorded, switching the user (which needs a session) is left out."""

from tests.project.settings_bare import *  # noqa: F403

INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "tracepanel.django"]
