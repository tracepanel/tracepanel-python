"""Requests: route, view, method, status, duration, user id.

Sync and async: under ASGI the middleware runs on the event loop, so async views are not pushed to a thread for it.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.utils.datastructures import MultiValueDict
from django.utils.functional import SimpleLazyObject, empty

from tracepanel.core import conf, errors, profile, w3c
from tracepanel.core import request as details
from tracepanel.django import console, signals, widget
from tracepanel.integrations import htmx

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser
from tracepanel.core.context import current_trace, start_trace
from tracepanel.core.model import Attrs, Trace
from tracepanel.storage.transport import complete

GetResponse = Callable[[HttpRequest], HttpResponseBase] | Callable[[HttpRequest], Awaitable[HttpResponseBase]]


def _loaded_user(request: HttpRequest) -> "AbstractBaseUser | AnonymousUser | None":
    """The user the view already loaded (request.user or `await request.auser()`), never a new lookup."""
    user = getattr(request, "user", None)
    # Reading a lazy user that the view never touched would run session and user queries.
    if isinstance(user, SimpleLazyObject) and user._wrapped is empty:  # noqa: SLF001
        user = None
    return user if user is not None else getattr(request, "_acached_user", None)


def user_attrs(request: HttpRequest) -> Attrs:
    return attrs_for_user(_loaded_user(request))


def attrs_for_user(user: object) -> Attrs:
    """`user.*` attributes of an already loaded user (requests, WebSocket scopes)."""
    if user is None or (isinstance(user, SimpleLazyObject) and user._wrapped is empty):  # noqa: SLF001
        return {}
    if not getattr(user, "is_authenticated", False):
        return {"user.id": None}
    # The username is local context (like locals); the agent drops it before anything leaves the machine.
    return {
        "user.id": str(getattr(user, "pk", "")),
        "user.username": str(user.get_username()) if hasattr(user, "get_username") else None,
        "user.is_staff": bool(getattr(user, "is_staff", False)),
        "user.is_superuser": bool(getattr(user, "is_superuser", False)),
    }


def route_template(route: str) -> str:
    """`users/<pk>/` for regex routes too (re_path, DRF routers): `users/(?P<pk>[^/.]+)/$` reads like a path()."""
    if not any(char in route for char in "^$?(\\"):
        return route
    out: list[str] = []
    i = 0
    while i < len(route):
        if route.startswith("(?P<", i):
            name_end = route.index(">", i)
            out.append(f"<{route[i + 4 : name_end]}>")
            depth, i = 1, name_end + 1
            while i < len(route) and depth:
                depth += {"(": 1, ")": -1}.get(route[i], 0) if route[i - 1] != "\\" else 0
                i += 1
            continue
        char = route[i]
        if char == "\\" and i + 1 < len(route):
            out.append(route[i + 1])
            i += 2
            continue
        if char not in "^$?":
            out.append(char)
        i += 1
    return "".join(out)


def finish(trace: Trace, request: HttpRequest, response: HttpResponseBase) -> None:
    match = request.resolver_match
    if match is not None:
        # path("") is the site root: its route is "", which still names an endpoint ("/").
        trace.attrs["http.route"] = f"/{route_template(match.route)}"
        # An API framework may have named the function behind its own dispatch view already (Django Ninja).
        trace.attrs.setdefault("code.view", match._func_path)  # noqa: SLF001
        trace.name = f"{request.method} /{route_template(match.route)}"
    operation = trace.attrs.get("graphql.operation")
    if isinstance(operation, str):
        # One URL serves every GraphQL operation: the operation names the request.
        trace.name = f"GraphQL {operation}"
    trace.attrs["http.status_code"] = response.status_code
    if conf.get_config().capture_request:
        trace.attrs.update(details.response_headers(response.items()))
        trace.attrs.update(_read_body(request))
        trace.attrs.update(_session(request))
    body = response.content if isinstance(response, HttpResponse) else None
    trace.attrs.update(htmx.response_attrs(trace.attrs, response.status_code, response.headers, body))
    if not trace.attrs.get("user.logged_out"):
        # After logout() request.user is anonymous: the user who left was recorded by the signal.
        trace.attrs.update(user_attrs(request))
    logged = errors.pop_logged_exception(trace.id)
    if response.status_code >= 500:  # noqa: PLR2004
        trace.status = "error"
        if not trace.errors and logged is not None:
            errors.capture(logged, handled=False)


def _attrs(request: HttpRequest) -> Attrs:
    attrs: Attrs = {
        "http.method": request.method,
        "http.path": request.path,
        **htmx.request_attrs(request.headers),
        # Called by another app: the W3C trace its request carried.
        **w3c.incoming(request.headers.get(w3c.HEADER)),
    }
    if conf.get_config().capture_request:
        attrs.update(details.request_headers(request.headers.items()))
        attrs.update(details.query(str(request.META.get("QUERY_STRING", ""))))
        attrs.update(details.cookie_values(request.COOKIES))
    return attrs


def _read_body(request: HttpRequest) -> Attrs:
    """The body as far as the view read it: Django keeps what it parsed or read, and Tracepanel reads nothing more.

    Private attributes, because request.POST and request.body would read the stream when the view didn't.
    """
    if hasattr(request, "_post"):
        files = getattr(request, "_files", None)
        fields = [(key, value) for key in request._post for value in request._post.getlist(key)]  # noqa: SLF001
        uploads = (
            [(key, upload.name or "", upload.size or 0) for key in files for upload in files.getlist(key)]
            if isinstance(files, MultiValueDict)
            else []
        )
        parsed = details.form(fields, uploads)
        if parsed:
            return parsed
    data = getattr(request, "_body", None)
    if isinstance(data, bytes):
        return details.body(request.content_type or "", data)
    return {}


def _session(request: HttpRequest) -> Attrs:
    """The session as the view left it, if it loaded one: reading it here would load it, and mark it accessed."""
    session = getattr(request, "session", None)
    loaded = getattr(session, "_session_cache", None)
    return details.session(loaded.items()) if isinstance(loaded, dict) else {}


def _profiled(request: HttpRequest) -> bool:
    """Whether the request asks to be profiled, by header or by the cookie a browser sends."""
    return profile.requested(request.headers.get(profile.HEADER), request.headers.get("cookie"))


class TracepanelMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> HttpResponseBase | Awaitable[HttpResponseBase]:
        if self.is_async:
            return self._acall(request)
        get_response: Callable[[HttpRequest], HttpResponseBase] = self.get_response  # type: ignore[assignment]
        config = conf.get_config()
        if not config.enabled:
            # Settings changed after startup (override_settings(DEBUG=False)): off until they change back.
            return get_response(request)
        if request.path_info.startswith(config.prefix):
            return console.dispatch(request, request.path_info.removeprefix(config.prefix))
        if conf.matches(request.path_info, config.ignore_paths) or current_trace() is not None:
            return get_response(request)

        with start_trace(
            "http", f"{request.method} {request.path}", _attrs(request), profile=_profiled(request)
        ) as trace:
            earlier = signals.adopt_request_started(trace)
            response = get_response(request)
            finish(trace, request, response)
        trace.duration = trace.spans[0].duration = trace.duration + earlier
        complete(trace)
        widget.decorate(request, response, trace, config)
        return response

    async def _acall(self, request: HttpRequest) -> HttpResponseBase:
        get_response: Callable[[HttpRequest], Awaitable[HttpResponseBase]] = self.get_response  # type: ignore[assignment]
        config = conf.get_config()
        if not config.enabled:
            return await get_response(request)
        if request.path_info.startswith(config.prefix):
            # The console's API reads SQLite (and user switching writes a session): off the event loop.
            return await sync_to_async(console.dispatch)(request, request.path_info.removeprefix(config.prefix))
        if conf.matches(request.path_info, config.ignore_paths) or current_trace() is not None:
            return await get_response(request)

        with start_trace(
            "http", f"{request.method} {request.path}", _attrs(request), profile=_profiled(request)
        ) as trace:
            earlier = signals.adopt_request_started(trace)
            response = await get_response(request)
            finish(trace, request, response)
        trace.duration = trace.spans[0].duration = trace.duration + earlier
        # Analysis is in-memory and the writer only enqueues: cheap enough for the event loop.
        complete(trace)
        widget.decorate(request, response, trace, config)
        return response
