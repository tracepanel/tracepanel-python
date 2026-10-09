import django
import pytest
from dirty_equals import IsPositiveInt, IsStr
from django.core import mail
from django.core.mail import send_mail
from django.test import Client
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tracepanel.core.context import start_trace
from tracepanel.core.model import Trace
from tracepanel.storage.store import Store
from tracepanel.storage.transport import StoreWriter, set_writer


def only(traces: list[Trace]) -> Trace:
    assert len(traces) == 1
    return traces[0]


@pytest.mark.django_db
def test_sent_email_is_a_span_with_its_content(
    client: Client, traces: list[Trace], validator: Draft202012Validator
) -> None:
    client.get("/welcome-email/")
    trace = only(traces)
    validator.validate(trace.to_dict())
    (span,) = [s for s in trace.spans if s.kind == "mail"]
    assert span.name == "mail Welcome to the blog"
    assert span.attrs["mail.to"] == ["alice@example.com"]
    assert span.attrs["mail.from"] == "hello@example.com"
    assert span.attrs["mail.sent"] == 1
    assert span.attrs["mail.body"] == "Hi Alice, thanks for joining."
    assert span.attrs["mail.html"] == "<h1>Hi Alice</h1><p>Thanks for joining.</p>"
    assert str(span.attrs["mail.backend"]).endswith("locmem.EmailBackend")
    assert len(mail.outbox) == 1  # the email itself still goes out


@pytest.mark.django_db
def test_mail_body_can_be_left_out(client: Client, traces: list[Trace], settings: Settings) -> None:
    settings.TRACEPANEL = {"capture_mail_body": False}
    client.get("/welcome-email/")
    (span,) = [s for s in only(traces).spans if s.kind == "mail"]
    assert "mail.body" not in span.attrs
    assert span.attrs["mail.subject"] == "Welcome to the blog"


@pytest.mark.django_db(databases=["default", "analytics"])
def test_database_and_cache_aliases(client: Client, traces: list[Trace]) -> None:
    client.get("/multi-backend/")
    trace = only(traces)
    aliases = {s.attrs["db.alias"] for s in trace.spans if s.kind == "sql"}
    assert aliases == {"default", "analytics"}
    (cache_span,) = [s for s in trace.spans if s.kind == "cache"]
    assert cache_span.attrs["cache.alias"] == "sessions"


@pytest.mark.django_db(databases=["default", "analytics"])
def test_alias_and_mail_filters(client: Client, store: Store) -> None:
    store.max_traces = 100
    writer = StoreWriter(store)
    set_writer(writer)
    try:
        for path in ("/multi-backend/", "/welcome-email/", "/duplicates/"):
            client.get(path)
        writer.flush()
    finally:
        set_writer(None)

    def paths(query: str) -> list[str]:
        return sorted(str(item["path"]) for item in store.search_traces(query)[0])

    assert paths("db:analytics") == ["/multi-backend/"]
    assert paths("cache:sessions") == ["/multi-backend/"]
    assert paths("has:mail") == ["/welcome-email/"]
    by_alias = {(str(q["alias"]), str(q["name"])) for q in store.list_queries("db:analytics")}
    assert by_alias == {("analytics", "SELECT COUNT auth_user")}
    listing = store.search_traces("has:mail")[0][0]
    assert listing["mails"] == 1


@pytest.mark.django_db(databases=["default", "analytics"])
def test_mail_listing(client: Client, store: Store) -> None:
    writer = StoreWriter(store)
    set_writer(writer)
    try:
        client.get("/welcome-email/")
        client.get("/duplicates/")
        writer.flush()
    finally:
        set_writer(None)
    expected = {
        "trace_id": IsStr,
        "span_id": IsStr,
        "time": IsPositiveInt,
        "subject": "Welcome to the blog",
        "sender": "hello@example.com",
        "recipients": ["alice@example.com"],
        "sent": True,
        "html": True,
        "trace_kind": "http",
        "trace_htmx": None,
        "method": "GET",
        "path": "/welcome-email/",
        "trace_name": "GET /welcome-email/",
    }

    assert store.list_mails() == ([expected], None)
    assert store.list_mails("to:alice@example.com") == ([expected], None)
    assert store.list_mails("to:alice@*") == ([expected], None)
    assert store.list_mails("Welcome from:hello@example.com since:1h") == ([expected], None)
    assert store.list_mails("to:bob@example.com") == ([], None)
    assert client.get("/__tracepanel__/api/mails").json() == {"mails": [expected], "next": None}


@pytest.mark.skipif(django.VERSION < (6, 1), reason="MAILERS and send(using=...) are Django 6.1+")
def test_mailers_name_the_backend(settings: Settings) -> None:
    settings.MAILERS = {
        "default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"},
        "transactional": {"BACKEND": "django.core.mail.backends.dummy.EmailBackend"},
    }

    with start_trace("command", "send") as trace:
        send_mail("Hi", "Body", "shop@example.com", ["alice@example.com"])
        send_mail("Hi", "Body", "shop@example.com", ["alice@example.com"], using="transactional")
    systems = [span.attrs["mail.system"] for span in trace.spans if span.kind == "mail"]

    # Not the SMTP that EMAIL_BACKEND's default would suggest: the backends MAILERS names.
    assert systems == ["locmem", "dummy"]
