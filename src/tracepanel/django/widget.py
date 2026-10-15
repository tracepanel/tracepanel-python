"""The widget on Django's responses: badge headers on every traced response, the script in HTML pages."""

from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from tracepanel.console import widget
from tracepanel.core.conf import Config
from tracepanel.core.model import Trace
from tracepanel.django import users


def decorate(request: HttpRequest, response: HttpResponseBase, trace: Trace, config: Config) -> None:
    api = request.META.get("SCRIPT_NAME", "").rstrip("/") + config.prefix
    origin = request.build_absolute_uri("/").rstrip("/")
    timing = widget.Timing.of(trace)
    page = widget.Page(
        request.method or "GET", response.status_code, origin, api, timing, response.charset, users=users.available()
    )
    headers = list(response.items())
    # A plain HttpResponse can get the script; streaming and file responses get the headers only.
    body = response.content if isinstance(response, HttpResponse) else None
    decorated = widget.decorate(trace, page, headers, body, config)
    for name, value in headers:
        if response.get(name) != value:
            response[name] = value
    if isinstance(response, HttpResponse) and decorated is not None and decorated is not body:
        response.content = decorated
