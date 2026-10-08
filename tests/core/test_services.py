import pytest

from tracepanel.core import services

DATABASES = ["default", "analytics"]


@pytest.mark.parametrize(
    ("location", "system", "attrs"),
    [
        (
            "redis://user:secret@cache.internal:6380/2",
            "redis",
            {"server.address": "cache.internal", "server.port": 6380, "db.namespace": "2"},
        ),
        ("redis://localhost", "redis", {"server.address": "localhost", "server.port": 6379}),
        ("127.0.0.1:11211", "memcached", {"server.address": "127.0.0.1", "server.port": 11211}),
        ("memcached.internal", "memcached", {"server.address": "memcached.internal", "server.port": 11211}),
        ("unix:/run/redis.sock", "redis", {"server.address": "/run/redis.sock"}),
        ("unix:///run/redis.sock?db=1", "redis", {"server.address": "/run/redis.sock"}),
    ],
)
def test_cache_location_keeps_host_port_and_number_only(location: str, system: str, attrs: dict[str, object]) -> None:
    assert services.location(location, system) == attrs


@pytest.mark.parametrize(
    ("url", "attrs"),
    [
        ("redis://:pw@broker:6379/0", {"messaging.system": "redis", "server.address": "broker", "server.port": 6379}),
        ("amqp://guest:pw@rabbit//", {"messaging.system": "rabbitmq", "server.address": "rabbit", "server.port": 5672}),
        ("", {"messaging.system": "rabbitmq", "server.address": "localhost", "server.port": 5672}),
        ("sqs://", {"messaging.system": "sqs"}),
        ("memory://", {"messaging.system": "memory"}),
        (
            "sentinel://s1:26379;sentinel://s2",
            {"messaging.system": "redis", "server.address": "s1", "server.port": 26379},
        ),
    ],
)
def test_broker_url(url: str, attrs: dict[str, object]) -> None:
    assert services.for_broker(url) == attrs


def test_database() -> None:
    assert services.database("postgresql", "", "", "blog") == {
        "db.system": "postgresql",
        "db.namespace": "blog",
        "server.address": "localhost",
        "server.port": 5432,
    }
    assert services.database("mysql", "db.internal", "3307", "shop") == {
        "db.system": "mysql",
        "db.namespace": "shop",
        "server.address": "db.internal",
        "server.port": 3307,
    }
    assert services.database("sqlite", "", "", "/srv/app/db.sqlite3") == {
        "db.system": "sqlite",
        "db.namespace": "db.sqlite3",
    }
    assert services.database("sqlite", "", "", ":memory:") == {"db.system": "sqlite", "db.namespace": None}
