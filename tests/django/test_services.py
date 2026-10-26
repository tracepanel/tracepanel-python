import pytest
from dirty_equals import IsInt, IsStr
from django.db import connection, connections
from django.test import Client
from pytest_django.fixtures import Settings

from tests.agentstore import AgentStore, newer
from tracepanel.core.model import Trace

DATABASES = ["default", "analytics"]


def aliases(node: dict[str, object]) -> list[str]:
    value = node["aliases"]
    assert isinstance(value, list)
    return [str(alias) for alias in value]


@pytest.mark.django_db(databases=DATABASES)
def test_calls_carry_their_service(client: Client, traces: list[Trace]) -> None:
    client.get("/multi-backend/")
    client.get("/welcome-email/")
    multi, mail = traces
    sql = next(span for span in multi.spans if span.kind == "sql")
    cache = next(span for span in multi.spans if span.kind == "cache")
    sent = next(span for span in mail.spans if span.kind == "mail")

    assert sql.attrs["db.system"] == connection.vendor
    assert (cache.attrs["cache.system"], sent.attrs["mail.system"]) == ("locmem", "locmem")
    assert (multi.attrs["service.name"], multi.attrs["service.role"]) == ("tracepanel-python", "web")


@pytest.mark.django_db
def test_smtp_server(traces: list[Trace], settings: Settings) -> None:
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST, settings.EMAIL_PORT, settings.EMAIL_TIMEOUT = "smtp.invalid", 2525, 0.01

    Client(raise_request_exception=False).get("/welcome-email/")
    (sent,) = [span for span in traces[0].spans if span.kind == "mail"]

    assert {key: sent.attrs[key] for key in ("mail.system", "server.address", "server.port")} == {
        "mail.system": "smtp",
        "server.address": "smtp.invalid",
        "server.port": 2525,
    }


@pytest.mark.django_db(databases=DATABASES)
def test_service_map(client: Client, traces: list[Trace], store: AgentStore, settings: Settings) -> None:
    settings.TRACEPANEL = {"service": "shop"}
    client.get("/multi-backend/")
    client.get("/welcome-email/")
    store.write(traces)
    result = store.list_services()
    peers = {node["id"] for node in result["services"]}

    databases = [node for node in result["services"] if node["kind"] == "database"]

    assert result["apps"] == [
        {
            "service": "shop",
            "role": "web",
            "traces": 2,
            "errors": 0,
            "total": IsInt(ge=0),
            "last_seen": IsInt,
            "runtime": IsStr(regex=r"Python 3\.\d+ · Django \d+\.\d+"),
        }
    ]
    assert [(node["kind"], node["system"]) for node in result["services"]] == [
        ("cache", "locmem"),
        *[("database", node["system"]) for node in databases],
        ("mail", "locmem"),
    ]
    # Each database node runs the engine of the aliases on it: with Postgres, `analytics` is still SQLite.
    assert {(alias, node["system"]) for node in databases for alias in aliases(node)} == {
        (alias, connections[alias].vendor) for alias in DATABASES
    }
    assert {(edge["service"], edge["role"], edge["peer"], edge["direction"]) for edge in result["edges"]} == {
        ("shop", "web", peer, "out") for peer in peers
    }

    (cache,) = store.list_services("kind:cache")["services"]

    assert cache == newer(
        {
            "id": "cache:locmem/sessions",
            "kind": "cache",
            "system": "locmem",
            "address": None,
            "port": None,
            "namespace": "sessions",
            "aliases": ["sessions"],
            "calls": 1,
            "total": IsInt(ge=0),
            "errors": 0,
            "last_seen": IsInt,
        },
        cache,
        {"cloud": None},
    )
