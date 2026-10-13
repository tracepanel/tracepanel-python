"""Django reports unhandled view exceptions with got_request_exception: they become the request's error."""

import sys

from django.core.signals import got_request_exception

from tracepanel.core.errors import capture


def on_request_exception(sender: object, **kwargs: object) -> None:  # noqa: ARG001 (signal API)
    exc = sys.exc_info()[1]
    if exc is not None:
        capture(exc, handled=False)


def install() -> None:
    got_request_exception.connect(on_request_exception, dispatch_uid="tracepanel.errors")
