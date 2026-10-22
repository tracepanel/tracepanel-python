import asyncio
import logging
import time
from collections.abc import AsyncIterator

from django.contrib.auth import login, logout
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache, caches
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.dispatch import Signal, receiver
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render

from tests.project import receivers  # noqa: F401 (connects the project's receivers)

logger = logging.getLogger(__name__)


def n_plus_one(request: HttpRequest) -> JsonResponse:
    # ForeignKey per row: SELECT ... FROM django_content_type WHERE id = %s
    labels = [p.content_type.app_label for p in Permission.objects.order_by("id")[:12]]
    # Many-to-many per row: SELECT ... FROM auth_group ... WHERE user_id = %s
    groups = [list(u.groups.all()) for u in User.objects.order_by("id")]
    return JsonResponse({"labels": len(labels), "groups": len(groups)})


def fixed(request: HttpRequest) -> JsonResponse:
    labels = [p.content_type.app_label for p in Permission.objects.select_related("content_type")[:12]]
    groups = [list(u.groups.all()) for u in User.objects.prefetch_related("groups")]
    return JsonResponse({"labels": len(labels), "groups": len(groups)})


def duplicates(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"counts": [User.objects.count() for _ in range(5)]})


def cached(request: HttpRequest) -> JsonResponse:
    cache.set("answer", 42)
    return JsonResponse({"hit": cache.get("answer"), "miss": cache.get("missing")})


def logs(request: HttpRequest) -> JsonResponse:
    logger.info("hello %s", "world")
    logger.warning("careful with %s", "fire", extra={"order_id": 42, "Payment-Token": "secret", "tags": ["a"]})
    try:
        int("x")
    except ValueError:
        logger.exception("parse failed")
    return JsonResponse({})


def error(request: HttpRequest) -> JsonResponse:
    divisor = int(request.GET.get("divisor", "0"))
    return JsonResponse({"value": User.objects.count() / divisor})


class PaymentError(Exception):
    pass


def saved_cards() -> dict[str, str]:
    return {}


def chained_error(request: HttpRequest) -> JsonResponse:
    api_token = "s3cr3t-value"  # masked in the captured locals
    amount = 42
    try:
        card = saved_cards()["default"]
    except KeyError as exc:
        msg = f"charge of {amount} failed with token {len(api_token)} chars"
        raise PaymentError(msg) from exc
    return JsonResponse({"card": card})


def me(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"username": request.user.get_username()})


def untouched_user(request: HttpRequest) -> JsonResponse:
    return JsonResponse({})


def multi_backend(request: HttpRequest) -> JsonResponse:
    caches["sessions"].set("visits", 1)
    return JsonResponse(
        {"main": User.objects.count(), "analytics": User.objects.using("analytics").count()},
    )


def welcome_email(request: HttpRequest) -> JsonResponse:
    sent = send_mail(
        "Welcome to the blog",
        "Hi Alice, thanks for joining.",
        "hello@example.com",
        ["alice@example.com"],
        html_message="<h1>Hi Alice</h1><p>Thanks for joining.</p>",
    )
    return JsonResponse({"sent": sent})


def users_page(request: HttpRequest) -> HttpResponse:
    # N+1 inside the template: user.groups.all per user.
    return render(request, "tests/users.html", {"users": User.objects.order_by("id")})


class Counted:
    """A value whose repr() runs a query: a template's context shows it without running it."""

    def __repr__(self) -> str:
        return f"<{User.objects.count()} users>"


def context_page(request: HttpRequest) -> HttpResponse:
    # One query for the groups (evaluated here), one for the first user; `users` is never evaluated.
    groups = Group.objects.order_by("id")
    list(groups)
    context = {
        "title": "Users",
        "users": User.objects.order_by("id"),
        "groups": groups,
        "first": User.objects.order_by("id").first(),
        "counted": Counted(),
        "api_key": "k-123",
    }
    return render(request, "tests/context.html", context)


def html(request: HttpRequest) -> HttpResponse:
    return HttpResponse("<html><body><h1>hi</h1></body></html>")


@receiver(post_save, sender=Group)
def group_saved(sender: type[Group], instance: Group, **kwargs: object) -> None:
    # Work inside a receiver: its query belongs to the receiver's span.
    User.objects.filter(groups=instance).count()


def create_group(request: HttpRequest) -> JsonResponse:
    group = Group.objects.create(name="editors")
    return JsonResponse({"id": group.pk})


def log_in(request: HttpRequest) -> JsonResponse:
    # user_logged_in: Django's own receiver (update_last_login) writes the user's last_login.
    login(request, User.objects.get(username="user0"), backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"user": request.user.get_username()})


def home(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"page": "home"})


def echo_json(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"read": len(request.body)})


def upload(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"fields": len(request.POST), "files": len(request.FILES)})


def remember(request: HttpRequest) -> JsonResponse:
    request.session["cart"] = [1, 2]
    request.session["oauth_token"] = "t-123"
    return JsonResponse({"saved": True})


def enqueue(request: HttpRequest) -> JsonResponse:
    from tests.project.jobs import count_users  # noqa: PLC0415 (Celery is an optional test group)

    result = count_users.delay("queued")
    return JsonResponse({"task": result.id})


def run_eager(request: HttpRequest) -> JsonResponse:
    from tests.project.jobs import count_users  # noqa: PLC0415

    return JsonResponse({"result": count_users.apply(("eager",)).get()})


def same_group(request: HttpRequest) -> JsonResponse:
    # Every user's first group is the same "staff" group: identical queries, still an N+1 (follows a relation).
    memberships = list(User.groups.through.objects.order_by("id"))
    return JsonResponse({"groups": [getattr(membership, "group").name for membership in memberships]})  # noqa: B009 (untyped through model)


def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


def instrumented(request: HttpRequest) -> JsonResponse:
    import tracepanel  # noqa: PLC0415

    with tracepanel.span("pricing.calculate", cart_id=7, attrs={"shop.region": "eu"}) as span:
        count = User.objects.count()
        span.set("total", count)
        span.set("items", ["a", "b"])
    tracepanel.set_attr("tenant", "acme", on_trace=True)
    tracepanel.event("cache warmed", keys=3)
    return JsonResponse({"count": score(count)})


def score(count: int) -> int:
    return count + 1


order_placed = Signal()


@receiver(order_placed)
async def notify_shop(sender: object, **kwargs: object) -> None:
    await User.objects.filter(is_staff=True).acount()


@receiver(order_placed)
def count_orders(sender: object, **kwargs: object) -> None:
    User.objects.count()


async def async_users(request: HttpRequest) -> JsonResponse:
    user = await request.auser()
    names = [member.username async for member in User.objects.order_by("id")]
    groups, staff = await asyncio.gather(Group.objects.acount(), User.objects.filter(is_staff=True).acount())
    await order_placed.asend(sender=None, order=1)
    return JsonResponse({"me": user.get_username(), "names": names, "groups": groups, "staff": staff})


async def async_error(request: HttpRequest) -> JsonResponse:
    count = await User.objects.acount()
    msg = f"failed after counting {count} users"
    raise ValueError(msg)


async def async_n_plus_one(request: HttpRequest) -> JsonResponse:
    # One aget() per permission for its content type, then the same count ten times.
    labels = []
    async for permission in Permission.objects.order_by("id").values("content_type_id")[:12]:
        content_type = await ContentType.objects.aget(pk=permission["content_type_id"])
        labels.append(content_type.app_label)
    counts = []
    for _ in range(5):
        counts.append(await User.objects.acount())  # noqa: PERF401 (a comprehension is its own frame on 3.11)
    return JsonResponse({"labels": len(labels), "counts": counts[0]})


def send_receipt() -> None:
    send_mail("Your receipt", "Thanks for the order.", "shop@example.com", ["ann@example.com"])


def place_order(request: HttpRequest) -> JsonResponse:
    with transaction.atomic():
        group = Group.objects.create(name="buyers")
        with transaction.atomic():
            User.objects.count()
        # Goes out even if the transaction rolls back: reported.
        send_mail("Order placed", "We got your order.", "shop@example.com", ["ann@example.com"])
        # After COMMIT: fine.
        transaction.on_commit(send_receipt)
    try:
        with transaction.atomic():
            Group.objects.create(name="buyers")
    except IntegrityError:
        pass
    return JsonResponse({"group": group.pk})


def caught_and_logged(request: HttpRequest) -> HttpResponse:
    # What django-ninja and custom exception handlers do: log the exception, answer 500.
    try:
        _ = 1 / int(request.GET.get("divisor", "0"))
    except ZeroDivisionError:
        logger.exception("payment failed")
        return HttpResponse("payment failed", status=500)
    return HttpResponse("ok")


def sign_out(request: HttpRequest) -> JsonResponse:
    logout(request)
    return JsonResponse({"ok": True})


def django_task(request: HttpRequest, kind: str) -> JsonResponse:
    from tests.project import django_jobs  # noqa: PLC0415 (Django Tasks are an optional test group)

    if kind == "inline":
        result = django_jobs.count_users.enqueue("inline")
    elif kind == "broken":
        result = django_jobs.broken.enqueue()
    else:
        result = django_jobs.queued_count.enqueue("queued")
    return JsonResponse({"id": str(result.id), "status": str(result.status)})


def streamed_page(request: HttpRequest) -> StreamingHttpResponse:
    del request
    return StreamingHttpResponse(iter([b"<html><he", b"ad><title>t</title></head>", b"<body>streamed</body></html>"]))


def async_streamed_page(request: HttpRequest) -> StreamingHttpResponse:
    del request

    async def pieces() -> AsyncIterator[bytes]:
        yield b"<html><head><title>t</title></head>"
        yield b"<body>streamed</body></html>"

    return StreamingHttpResponse(pieces())


def html_page(request: HttpRequest) -> HttpResponse:
    return HttpResponse(
        '<!doctype html><html><head><title>App</title><script src="/app.js"></script></head>'
        "<body><h1>hi</h1></body></html>"
    )


def htmx_row(request: HttpRequest) -> HttpResponse:
    # A fragment for htmx, with an event for the page.
    response = HttpResponse("<tr><td>row</td></tr>")
    response["HX-Trigger"] = "rowSaved"
    return response


def htmx_login_needed(request: HttpRequest) -> HttpResponseRedirect:
    # What login_required does: htmx follows it and swaps the login page in.
    return redirect("/html-page/")


def spin(seconds: float) -> int:
    """Busy in project code for a while: what a profile shows."""
    end = time.perf_counter() + seconds
    turns = 0
    while time.perf_counter() < end:
        turns += 1
    return turns


def busy(request: HttpRequest) -> HttpResponse:
    del request
    spin(0.15)
    return HttpResponse("done")
