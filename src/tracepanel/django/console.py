"""The console under Django: requests to tracepanel's prefix go to the framework-neutral console app.

Also registers what only Django can do: switching the user (sessions, auth) and EXPLAIN on its databases.
"""

from collections.abc import Callable

from django.apps import apps
from django.http import FileResponse, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from tracepanel.console import app
from tracepanel.django import explain, users
from tracepanel.integrations import dbapi


def dispatch(request: HttpRequest, path: str) -> HttpResponseBase:
    response = app.dispatch(
        app.Request(method=request.method or "GET", path=path, query=request.GET.dict(), native=request)
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
    app.register("explain", app.explain_handler(dbapi.explain_or(explain.explain)))
    # Switching the user needs Django's auth and sessions: without them the API (and the widget's button) is absent.
    if apps.is_installed("django.contrib.auth") and apps.is_installed("django.contrib.sessions"):
        app.register("users", _view(users.search))
        app.register("users/me", _view(users.me))
        app.register("users/switch", _view(users.switch), write=True)
