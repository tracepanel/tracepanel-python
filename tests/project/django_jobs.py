"""Django Tasks for the tests: the same functions on the immediate and the database backend."""

from collections.abc import Callable

from django.contrib.auth.models import User
from django.db import transaction

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


# What a test runs while the worker runs `watched`: it looks at the console meanwhile.
during: list[Callable[[], None]] = []


@task(backend="database")
def watched() -> None:
    for look in during:
        look()


@task
def fan_out() -> str:
    # Enqueues another task from inside its run: on the immediate backend, it runs right there.
    return str(count_users.enqueue("inner").id)


@task
def groups_per_user() -> int:
    # N+1 inside a task: the groups of every user are loaded one by one.
    return sum(len(list(user.groups.all())) for user in User.objects.order_by("id"))


@task
def counted_in_a_transaction() -> int:
    with transaction.atomic():
        return User.objects.count()


@task(backend="database")
def queued_in_a_transaction() -> int:
    with transaction.atomic():
        return User.objects.count()
