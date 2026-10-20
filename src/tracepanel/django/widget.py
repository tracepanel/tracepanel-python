"""The widget on Django's responses: badge headers on every traced response, the script in HTML pages."""

from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from tracepanel.console import widget
from tracepanel.core.conf import Config
from tracepanel.core.model import Trace


def decorate(request: HttpRequest, response: HttpResponseBase, trace: Trace, config: Config) -> None:
    api = request.META.get("SCRIPT_NAME", "").rstrip("/") + config.prefix
    origin = request.build_absolute_uri("/").rstrip("/")
    timing = widget.Timing.of(trace)
    requested = {name.lower(): value for name, value in request.headers.items()}
    page = widget.Page(
        request.method or "GET",
        response.status_code,
        origin,
        api,
        timing,
        response.charset,
        requested,
        cookies_in_headers=False,
    )
    if (redirect := widget.redirected(trace, page)) is not None:
        response.set_cookie(widget.REDIRECTS, redirect, max_age=30, path="/", samesite="Lax")
    headers = list(response.items())
    # A plain HttpResponse can get the script; streaming and file responses get the headers only.
    body = response.content if isinstance(response, HttpResponse) else None
    decorated = widget.decorate(trace, page, headers, body, config)
    for name, value in headers:
        if response.get(name) != value:
            response[name] = value
    if isinstance(response, HttpResponse) and decorated is not None and decorated is not body:
        response.content = decorated
