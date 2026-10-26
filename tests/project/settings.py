import importlib.util
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
SECRET_KEY = "tracepanel-tests"
DEBUG = True
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "tracepanel.django",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
ROOT_URLCONF = "tests.project.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "tests" / "project" / "templates"],
    }
]
DATABASES: dict[str, dict[str, object]] = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DATABASES["analytics"] = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
if os.environ.get("TRACEPANEL_TEST_DB") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "tracepanel"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        },
        "analytics": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    }
# SQL Server through mssql-django, next to the default database (tests/django/test_mssql.py).
if os.environ.get("TRACEPANEL_TEST_MSSQL") and importlib.util.find_spec("mssql"):
    DATABASES["mssql"] = {
        "ENGINE": "mssql",
        "NAME": os.environ.get("MSSQL_DATABASE", "tracepanel"),
        "USER": os.environ.get("MSSQL_USER", "sa"),
        "PASSWORD": os.environ.get("MSSQL_PASSWORD", "Tracepanel-1"),
        "HOST": os.environ.get("MSSQL_HOST", "127.0.0.1"),
        "PORT": os.environ.get("MSSQL_PORT", "1433"),
        "OPTIONS": {
            "driver": os.environ.get("MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
            "extra_params": "TrustServerCertificate=yes",
        },
        # Its test database alone, without the default one's.
        "TEST": {"DEPENDENCIES": list[str]()},
    }
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "sessions": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "sessions"},
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {"tests": {"level": "INFO"}},
}

# Channels tests: an in-memory layer (group messages within the test process).
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
ASGI_APPLICATION = "tests.project.sockets.application"

# Django Tasks tests: the framework (django.tasks on Django 6+, the django_tasks backport before) with an immediate
# default backend and the database backend from django-tasks-db.
if importlib.util.find_spec("django_tasks_db"):
    TASKS_MODULE = "django.tasks" if importlib.util.find_spec("django.tasks") else "django_tasks"
    INSTALLED_APPS += ["django_tasks_db"] + (["django_tasks"] if TASKS_MODULE == "django_tasks" else [])
    TASKS = {
        "default": {"BACKEND": f"{TASKS_MODULE}.backends.immediate.ImmediateBackend"},
        "database": {"BACKEND": "django_tasks_db.DatabaseBackend"},
    }

# django-q2 tests: the ORM broker on the test database, no Redis; the tests run the cluster's pieces in-process.
if importlib.util.find_spec("django_q"):
    INSTALLED_APPS += ["django_q"]
    Q_CLUSTER = {"orm": "default", "timeout": 30, "retry": 60}
