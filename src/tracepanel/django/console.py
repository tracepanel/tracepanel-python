"""The console under Django: requests to tracepanel's prefix go to the framework-neutral console app.

Also registers what only Django can do: switching the user (sessions, auth) and EXPLAIN on its databases.
"""

from collections.abc import Callable

from django.apps import apps
from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import connections
from django.http import FileResponse, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from tracepanel.console import app
from tracepanel.console.messages import MAX_BODY
from tracepanel.django import explain
from tracepanel.integrations import dbapi

LOCAL = frozenset({"127.0.0.1", "::1"})


def from_this_machine(request: HttpRequest) -> bool:
    """From this machine, or from an address the project lists as its own (INTERNAL_IPS)."""
    address = request.META.get("REMOTE_ADDR", "")
    return address in LOCAL or address in getattr(settings, "INTERNAL_IPS", ())


def _body(request: HttpRequest) -> tuple[bytes, bool]:
    """A POST's body, and whether it was too large to read (over the console's limit, or Django's own)."""
    if request.method != "POST":
        return b"", False
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY:
        return b"", True
    try:
        return request.body, False
    except RequestDataTooBig:
        return b"", True


def dispatch(request: HttpRequest, path: str) -> HttpResponseBase:
    body, too_large = _body(request)
    response = app.dispatch(
        app.Request(
            method=request.method or "GET",
            path=path,
            query=request.GET.dict(),
            native=request,
            body=body,
            content_type=request.content_type or "" if body else "",
            local=from_this_machine(request),
            too_large=too_large,
        )
    )
    if isinstance(response.native, HttpResponseBase):
        return response.native
    if response.file is not None:
        out: HttpResponseBase = FileResponse(response.file, content_type=response.content_type, status=response.status)
    else:
        out = HttpResponse(response.body, content_type=response.content_type, status=response.status)
    for name, value in response.headers.items():
        out[name] = value
    return out


def _view(view: Callable[[HttpRequest], HttpResponseBase]) -> app.Handler:
    """A Django view as a console handler: its response (cookies included) goes back as is."""

    def handle(request: app.Request) -> app.Response:
        native = request.native
        if not isinstance(native, HttpRequest):
            return app.json_response(app.NOT_FOUND, status=404)
        return app.Response(native=view(native))

    return handle


def install() -> None:
    # Asked by the agent, in a thread of its own: the connection Django opened there is closed after.
    app.explain_with(dbapi.explain_or(explain.explain), done=connections.close_all)
    # Switching the user needs Django's auth and sessions: without them the API (and the widget's button) is absent,
    # and the module, which imports auth's models, is never imported.
    if apps.is_installed("django.contrib.auth") and apps.is_installed("django.contrib.sessions"):
        from tracepanel.django import users  # noqa: PLC0415

        app.register("users", _view(users.search))
        app.register("users/me", _view(users.me))
        app.register("users/switch", _view(users.switch), write=True)
        app.capability("users", users.available)
