"""The test project with a custom user model (tests/django/custom_user): AUTH_USER_MODEL can't change mid-run."""

from tests.project.settings import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS, "tests.project.members"]  # noqa: F405
AUTH_USER_MODEL = "members.Member"
