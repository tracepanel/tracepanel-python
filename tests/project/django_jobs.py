"""Django Tasks for the tests: the same functions on the immediate and the database backend."""

from django.contrib.auth.models import User

try:
    from django.tasks import task
except ImportError:  # Django 5.2: the backport
    from django_tasks import task


@task
def count_users(label: str) -> str:
    return f"{label}: {User.objects.count()}"


@task
def broken() -> None:
    msg = "bounce list unavailable"
    raise ValueError(msg)


@task(backend="database")
def queued_count(label: str) -> str:
    return f"{label}: {User.objects.count()}"
