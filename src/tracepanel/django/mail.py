"""Outgoing email: every EmailMessage.send() (send_mail, EmailMultiAlternatives, …) becomes a `mail` span.

Locally the message itself is kept (text and HTML, truncated) so you can read what a request sent. It is development
data only: the agent strips mail.body / mail.html and recipient addresses before anything leaves the machine.
"""

import functools
from typing import Any

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.message import EmailMessage

from tracepanel.core import conf, services
from tracepanel.core.context import current_trace, start_span
from tracepanel.core.frames import origin
from tracepanel.core.model import Attrs
from tracepanel.core.patching import patch, patched

MAX_BODY = 64 * 1024


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_BODY else f"{text[:MAX_BODY]}\n… (truncated)"


def _html(message: EmailMessage) -> str | None:
    for content, mimetype in getattr(message, "alternatives", []) or []:
        if mimetype == "text/html":
            return str(content)
    return str(message.body) if getattr(message, "content_subtype", "plain") == "html" else None


DJANGO_BACKENDS = "django.core.mail.backends."
SMTP_BACKEND = f"{DJANGO_BACKENDS}smtp.EmailBackend"


def _service(connection: object, backend: str) -> Attrs:
    """SMTP server for the smtp backend; otherwise the backend's short name (console, locmem, anymail…)."""
    if backend == SMTP_BACKEND:
        if connection is not None:
            return services.for_smtp(getattr(connection, "host", None), getattr(connection, "port", None))
        return services.for_smtp(settings.EMAIL_HOST, settings.EMAIL_PORT)
    if backend.startswith(DJANGO_BACKENDS):
        return {"mail.system": backend.removeprefix(DJANGO_BACKENDS).split(".", 1)[0]}
    return {"mail.system": backend.split(".", 1)[0]}


def _connection(message: EmailMessage, using: str | None) -> object:
    """The backend this send goes through, picked as Django picks it.

    `using` (6.1+ MAILERS), the message's own connection (`_connection` in 6.1+, `connection` before), else the
    default (MAILERS or EMAIL_BACKEND).
    """
    if using is not None:
        from django.core.mail import mailers  # noqa: PLC0415 (Django 6.1+ only)

        return mailers[using]
    own = getattr(message, "_connection", None) or getattr(message, "connection", None)
    if own is not None:
        return own
    # Builds the backend object only: nothing connects until it sends.
    return get_connection()


def attrs_for(message: EmailMessage, using: str | None = None) -> Attrs:
    connection = _connection(message, using)
    backend = f"{type(connection).__module__}.{type(connection).__qualname__}"
    attrs: Attrs = {
        "mail.subject": str(message.subject),
        "mail.from": message.from_email,
        "mail.to": list(message.to),
        "mail.cc": list(message.cc),
        "mail.bcc_count": len(message.bcc),
        "mail.attachments": len(message.attachments),
        "mail.backend": backend,
        **_service(connection, backend),
    }
    if conf.get_config().capture_mail_body:
        html = _html(message)
        is_html = getattr(message, "content_subtype", "plain") == "html"
        attrs["mail.body"] = None if is_html else _truncate(str(message.body))
        attrs["mail.html"] = _truncate(html) if html else None
    return attrs


def install() -> None:
    if patched(EmailMessage, "send"):
        return
    original = EmailMessage.send

    @functools.wraps(original)
    def send(self: EmailMessage, *args: Any, **kwargs: Any) -> int:  # noqa: ANN401
        # Whatever Django's version takes (6.1 added `using=` for email providers) goes through unchanged.
        if current_trace() is None:
            return original(self, *args, **kwargs)
        subject = str(self.subject) or "(no subject)"
        attrs = {**attrs_for(self, kwargs.get("using")), "code.origin": origin()}
        with start_span("mail", f"mail {subject[:80]}", attrs) as span:
            sent = original(self, *args, **kwargs)
            if span is not None:
                span.attrs["mail.sent"] = sent
            return sent

    patch(EmailMessage, "send", send)
