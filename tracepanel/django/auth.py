"""Logins and logouts: after logout() request.user is anonymous, so the user who left is recorded when it happens."""

from django.contrib.auth.signals import user_logged_out

from tracepanel.core.context import current_trace
from tracepanel.django.middleware import attrs_for_user


def on_logged_out(sender: object, user: object = None, **kwargs: object) -> None:  # noqa: ARG001 (signal API)
    trace = current_trace()
    if trace is None or user is None:
        return
    trace.attrs.update(attrs_for_user(user))
    trace.attrs["user.logged_out"] = True


def install() -> None:
    user_logged_out.connect(on_logged_out, dispatch_uid="tracepanel.auth")
