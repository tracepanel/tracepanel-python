"""A Celery app for the tests: an in-memory broker, so a real worker can run in a thread."""

import threading
from typing import TYPE_CHECKING, Any

from celery import Celery
from celery.backends.cache import CacheBackend
from django.contrib.auth.models import Group, User

from tracepanel._compat import override

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


@app.task
def summarize(results: list[str]) -> str:
    # A chord's callback: the results of its header, in order.
    return ", ".join(results)


@app.task
def fan_out() -> str:
    # A task that queues another from inside its run.
    return count_users.delay("inner").id


@app.task(soft_time_limit=2)
def overruns() -> None:
    # What billiard raises in the task when its soft time limit passes (the solo pool has no time limits).
    from billiard.exceptions import SoftTimeLimitExceeded  # noqa: PLC0415

    raise SoftTimeLimitExceeded


@app.task
def make_group() -> int:
    # post_save of Group has a receiver (tests.project.views.group_saved): a signal span inside the task's trace.
    return Group.objects.create(name="from a task").pk


class StartedFails(CacheBackend):
    """A result backend that fails storing STARTED as a soft time limit can make it: outside the task's body.

    Celery imports its result backend when a pool process first uses it, inside the run's time limits: a soft limit
    hitting that import raises SoftTimeLimitExceeded there.
    """

    @override
    def store_result(self, task_id: str, result: object, state: str, *args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        if state == "STARTED":
            from billiard.exceptions import SoftTimeLimitExceeded  # noqa: PLC0415

            raise SoftTimeLimitExceeded
        return super().store_result(task_id, result, state, *args, **kwargs)


@app.task(track_started=True, soft_time_limit=3)
def cut_short() -> None:
    pass


cut_short.backend = StartedFails(app, backend="memory")


@app.task
def hurried(text: str) -> str:
    return text


# A soft time limit of milliseconds: it can pass while Celery's task_prerun is still running.
hurried.soft_time_limit = 0.005  # type: ignore[assignment]  # Celery takes a float, its stubs say int
