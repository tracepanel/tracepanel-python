"""A Celery app for the tests: an in-memory broker, so a real worker can run in a thread."""

import threading
from typing import TYPE_CHECKING

from celery import Celery
from django.contrib.auth.models import User

if TYPE_CHECKING:
    from celery import Task

app = Celery("tests", broker="memory://", backend="cache+memory://")
app.conf.update(task_store_eager_result=True)


@app.task
def count_users(prefix: str = "users") -> str:
    return f"{prefix}: {User.objects.count()}"


@app.task
def groups_per_user() -> int:
    # N+1 inside a task: the groups of every user are loaded one by one.
    return sum(len(list(user.groups.all())) for user in User.objects.order_by("id"))


@app.task
def broken() -> None:
    message = "boom"
    raise ValueError(message)


@app.task(bind=True, max_retries=1)
def flaky(self: "Task[[], str]") -> str:
    if self.request.retries == 0:
        raise self.retry(exc=ConnectionError("CRM down"), countdown=0)
    return f"ok after {self.request.retries} retry"


# Holds `waits` running until a test lets it go: the console lists it as running meanwhile.
release = threading.Event()


@app.task
def waits() -> bool:
    return release.wait(10)
