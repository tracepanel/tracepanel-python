"""A project without Django's optional apps (auth, contenttypes, sessions, admin, messages): tracepanel still runs."""

SECRET_KEY = "tracepanel-tests"
DEBUG = True
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = ["tracepanel.django"]
MIDDLEWARE: list[str] = []
ROOT_URLCONF = "tests.project.urls_bare"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
