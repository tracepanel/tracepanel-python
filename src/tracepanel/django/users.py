"""Switch the logged-in user from the widget: see the app as another user without logging out and in.

Development only, like the rest of tracepanel, and further limited to requests from this machine (or INTERNAL_IPS)
that come from the page itself: a same-origin fetch carrying the X-Tracepanel header, which another site can't send
without CORS. tracepanel answers before Django's session and auth middleware run, so the session is loaded and
saved here.
"""

import json
from importlib import import_module

from django.apps import apps
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, get_user, get_user_model, login, logout
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpRequest, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme

from tracepanel.core import conf

LOCAL = frozenset({"127.0.0.1", "::1"})
LIMIT = 20
SEARCH_FIELDS = ("email", "first_name", "last_name")


def available() -> bool:
    # Logging in as someone else stays a DEBUG-only feature, even when tracepanel is turned on without DEBUG.
    return (
        bool(settings.DEBUG)
        and conf.get_config().user_switch
        and apps.is_installed("django.contrib.auth")
        and apps.is_installed("django.contrib.sessions")
    )


def from_this_machine(request: HttpRequest) -> bool:
    """From this machine, or from an address the project lists as its own (INTERNAL_IPS)."""
    address = request.META.get("REMOTE_ADDR", "")
    return address in LOCAL or address in getattr(settings, "INTERNAL_IPS", ())


def trusted(request: HttpRequest) -> bool:
    """From this machine (or INTERNAL_IPS), and for writes, from the page itself."""
    if not from_this_machine(request):
        return False
    if request.method == "GET":
        return True
    origin = request.headers.get("Origin")
    same_origin = origin is None or url_has_allowed_host_and_scheme(origin, allowed_hosts={request.get_host()})
    return request.headers.get("X-Tracepanel") == "1" and same_origin


def _session(request: HttpRequest) -> None:
    engine = import_module(settings.SESSION_ENGINE)
    request.session = engine.SessionStore(request.COOKIES.get(settings.SESSION_COOKIE_NAME))


def describe(user: object) -> dict[str, object] | None:
    if not getattr(user, "is_authenticated", False):
        return None
    full_name = getattr(user, "get_full_name", lambda: "")()
    return {
        "id": str(getattr(user, "pk", "")),
        "username": str(getattr(user, "get_username", lambda: "")()),
        "name": full_name or None,
        "email": getattr(user, "email", None) or None,
        "is_staff": bool(getattr(user, "is_staff", False)),
        "is_superuser": bool(getattr(user, "is_superuser", False)),
    }


def _guard(request: HttpRequest) -> JsonResponse | None:
    if not available():
        return JsonResponse({"error": "user switching needs DEBUG, django.contrib.auth and sessions"}, status=404)
    if not trusted(request):
        return JsonResponse(
            {"error": "user switching only works from the tracepanel widget on this machine"}, status=403
        )
    return None


def me(request: HttpRequest) -> JsonResponse:
    if refused := _guard(request):
        return refused
    _session(request)
    return JsonResponse({"user": describe(get_user(request))})


def search(request: HttpRequest) -> JsonResponse:
    if refused := _guard(request):
        return refused
    model = get_user_model()
    field = str(getattr(model, "USERNAME_FIELD", "username"))
    users = model._default_manager.all()  # noqa: SLF001 (the model's own manager, whatever its name)
    if term := request.GET.get("q", "").strip():
        # The username, and the name and email when the user model has them (custom models may not).
        names = {f.name for f in model._meta.get_fields()}  # noqa: SLF001 (Django's model API)
        fields = [field, *(name for name in SEARCH_FIELDS if name in names and name != field)]
        match = Q()
        for name in fields:
            match |= Q(**{f"{name}__icontains": term})
        users = users.filter(match)
    users = users.order_by(field)[:LIMIT]
    return JsonResponse({"users": [describe(user) for user in users]})


def switch(request: HttpRequest) -> JsonResponse:
    if refused := _guard(request):
        return refused
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    try:
        user_id = json.loads(request.body or b"{}").get("user_id")
    except (ValueError, AttributeError):
        return JsonResponse({"error": 'expected {"user_id": ...}'}, status=400)
    _session(request)
    request.user = get_user(request)
    if user_id is None:
        logout(request)
        user: object = AnonymousUser()
    else:
        try:
            found = get_user_model()._default_manager.filter(pk=user_id).first()  # noqa: SLF001
        except (ValidationError, ValueError, TypeError):
            # Not a key of this model at all ("abc" for an integer or UUID key): no such user either.
            found = None
        if found is None:
            return JsonResponse({"error": "no such user"}, status=404)
        backend = request.session.get(BACKEND_SESSION_KEY) or settings.AUTHENTICATION_BACKENDS[0]
        login(request, found, backend=backend)
        user = found
    request.session.save()
    response = JsonResponse({"user": describe(user)})
    key = request.session.session_key
    if key is not None:
        # What SessionMiddleware would do on the way out.
        expiry = None if request.session.get_expire_at_browser_close() else request.session.get_expiry_age()
        response.set_cookie(
            settings.SESSION_COOKIE_NAME,
            key,
            max_age=expiry,
            domain=settings.SESSION_COOKIE_DOMAIN,
            path=settings.SESSION_COOKIE_PATH,
            secure=bool(settings.SESSION_COOKIE_SECURE),
            httponly=bool(settings.SESSION_COOKIE_HTTPONLY),
            samesite=settings.SESSION_COOKIE_SAMESITE,
        )
    return response
