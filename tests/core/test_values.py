"""Settings and a template's context as the console shows them: repr()-ed, cut, secrets masked."""

from collections.abc import Callable

import pytest
from dirty_equals import IsStr

from tracepanel.core import values
from tracepanel.core.frames import MASK


@pytest.mark.parametrize(
    "name",
    [
        "SECRET_KEY",
        "SECRET_KEY_FALLBACKS",
        "DB_PASSWORD",
        "EMAIL_HOST_PASSWORD",
        "STRIPE_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "SIGNING_SIGNATURE",
        "JWT_PRIVATE_PEM",
        "GOOGLE_CREDENTIALS",
        "password",
        "TEST_CARD_NUMBER",
        "STRIPE_TEST_CVC",
    ],
)
def test_a_setting_with_a_secret_name_is_masked_whole(name: str) -> None:
    assert values.setting_repr(name, "hunter2") == MASK


@pytest.mark.parametrize(
    "name",
    [
        # Django's own, whose names only look secret.
        "PASSWORD_HASHERS",
        "AUTH_PASSWORD_VALIDATORS",
        "PASSWORD_RESET_TIMEOUT",
        # What the locals' rule would mask: sessions, cookies, CSRF, auth say nothing secret as settings.
        "SESSION_COOKIE_AGE",
        "CSRF_COOKIE_NAME",
        "AUTH_USER_MODEL",
        "DEBUG",
    ],
)
def test_a_setting_with_a_plain_name_is_shown(name: str) -> None:
    assert values.setting_repr(name, "x") == '"x"'


def test_secrets_inside_a_setting_are_masked_at_any_depth() -> None:
    databases = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "shop",
            "USER": "shop",
            "PASSWORD": "pw",
            "OPTIONS": {"sslmode": "require", "password": "pw"},
        }
    }
    caches = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "KEY_PREFIX": "shop",
            "KEY_FUNCTION": "shop.cache.key",
            "OPTIONS": {"password": "pw"},
        }
    }

    assert values.setting_repr("DATABASES", databases) == (
        '{"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "shop", "OPTIONS": {"password": ********, '
        '"sslmode": "require"}, "PASSWORD": ********, "USER": "shop"}}'
    )

    assert values.setting_repr("CACHES", caches) == (
        '{"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "KEY_FUNCTION": "shop.cache.key", '
        '"KEY_PREFIX": "shop", "OPTIONS": {"password": ********}}}'
    )

    assert values.setting_repr("LIST", [("ann", {"token": "t"})]) == '[("ann", {"token": ********})]'


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("https://abc123@o1.ingest.sentry.io/42", '"https://********@o1.ingest.sentry.io/42"'),
        ("postgres://shop:pw@db:5432/shop", '"postgres://********@db:5432/shop"'),
        ("redis://:pw@cache:6379/0", '"redis://********@cache:6379/0"'),
        # No credentials: as it is.
        ("https://example.com/path?a=1", '"https://example.com/path?a=1"'),
        ("ann@example.com", '"ann@example.com"'),
    ],
)
def test_the_credentials_in_a_url_are_masked_whatever_its_name(value: str, shown: str) -> None:
    assert values.setting_repr("SENTRY_DSN", value) == shown

    assert values.setting_repr("BROKERS", [value]) == f"[{shown}]"


def test_a_setting_is_cut_past_its_limits() -> None:
    assert values.setting_repr("INSTALLED_APPS", [f"app{i}" for i in range(30)]) == (
        "[" + ", ".join(f'"app{i}"' for i in range(30)) + "]"
    )

    assert values.setting_repr("BIG", "x" * 600) == f'"{"x" * 247}...{"x" * 248}"'

    shown = values.setting_repr("HUGE", [["y" * 400] * 20] * 3)
    assert (len(shown), shown[-3:]) == (values.SETTING_MAX, "...")


def test_a_broken_repr_is_named_not_raised() -> None:
    class Broken:
        def __repr__(self) -> str:
            raise RuntimeError

    # As an error's local variables show one.
    assert values.setting_repr("THING", Broken()) == IsStr(regex=r"<Broken instance at 0x[0-9a-f]+>")


def test_settings_are_the_uppercase_names_sorted() -> None:
    shown = values.settings_reprs({"DEBUG": True, "lower": 1, "ALLOWED_HOSTS": ["*"], "SECRET_KEY": "s"})

    assert shown == {"ALLOWED_HOSTS": '["*"]', "DEBUG": "True", "SECRET_KEY": MASK}


def test_a_cards_number_and_code_are_masked_in_a_setting_and_a_context() -> None:
    card = {"number": "4242424242424242", "cvc": "123", "exp_year": 2030}

    assert values.setting_repr("PAYMENT_DEFAULTS", {"card": card}) == (
        '{"card": {"cvc": ********, "exp_year": 2030, "number": ********}}'
    )

    assert values.context_attrs({"card": card, "card_number": "4242"}) == {
        "template.context.names": ["card", "card_number"],
        "template.context.values": ['{"cvc": ********, "exp_year": 2030, "number": ********}', MASK],
    }


def test_a_cards_number_beside_its_other_fields_is_masked_in_a_setting_and_a_context() -> None:
    source = {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}

    assert values.setting_repr("STRIPE_TEST_SOURCES", [source]) == (
        '[{"cvc": ********, "exp_month": 12, "exp_year": 2030, "number": ********}]'
    )

    assert values.setting_repr("TEST_PAYMENT", {"number": "4242 4242 4242 4242", "amount": 100}) == (
        '{"amount": 100, "number": ********}'
    )

    assert values.context_attrs(
        {"source": source, "number": "1", "exp_year": 2030, "order": {"number": 17, "author": "ann"}}
    ) == {
        "template.context.names": ["source", "number", "exp_year", "order"],
        "template.context.values": [
            '{"cvc": ********, "exp_month": 12, "exp_year": 2030, "number": ********}',
            MASK,
            "2030",
            '{"author": "ann", "number": 17}',
        ],
    }


def test_an_authors_name_is_shown_in_a_context() -> None:
    attrs = values.context_attrs({"author": "Ann", "authors": ["Ann", "Bob"], "book": {"author_id": 7}})

    assert attrs == {
        "template.context.names": ["author", "authors", "book"],
        "template.context.values": ['"Ann"', '["Ann", "Bob"]', '{"author_id": 7}'],
    }


def test_tokens_in_a_context_are_masked_unless_they_are_a_models_counts() -> None:
    attrs = values.context_attrs({"tokens": {"prompt": 12}, "user": {"tokens": ["abc"], "max_tokens": 1024}})

    assert attrs == {
        "template.context.names": ["tokens", "user"],
        "template.context.values": ['{"prompt": 12}', '{"max_tokens": 1024, "tokens": ********}'],
    }


def test_a_context_keeps_its_names_in_order_with_the_locals_rules() -> None:
    attrs = values.context_attrs(
        {
            "title": "Books",
            "csrf_token": "abc",
            "session": {"cart": 1},
            "config": {"SECRET_KEY": "s", "DEBUG": True},
            "books": list(range(20)),
            "__builtins__": {},
            "db": "postgres://shop:pw@db/shop",
        }
    )

    assert attrs == {
        "template.context.names": ["title", "csrf_token", "session", "config", "books", "db"],
        "template.context.values": [
            '"Books"',
            MASK,
            MASK,
            '{"DEBUG": True, "SECRET_KEY": ********}',
            "[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, ...]",
            '"postgres://********@db/shop"',
        ],
    }


def test_a_context_past_its_limit_says_how_many_were_left_out() -> None:
    attrs = values.context_attrs({f"name{i}": i for i in range(45)})

    assert attrs == {
        "template.context.names": [f"name{i}" for i in range(40)],
        "template.context.values": [str(i) for i in range(40)],
        "template.context.omitted": 5,
    }


class Lazy:
    """An object a framework shows by a summary: its repr() would do work."""

    def __repr__(self) -> str:
        raise AssertionError


class Querying:
    def __repr__(self) -> str:
        raise values.Blocked


@pytest.fixture
def frameworks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(values, "_summarizers", [])
    monkeypatch.setattr(values, "_guards", [])


def test_a_framework_summarizes_its_objects_and_guards_the_capture(frameworks: None) -> None:
    entered: list[str] = []

    def guard() -> Callable[[], None]:
        entered.append("in")
        return lambda: entered.append("out")

    values.summarize(lambda value: "<lazy, not evaluated>" if isinstance(value, Lazy) else None)
    values.guard(guard)

    attrs = values.context_attrs({"lazy": Lazy(), "inside": [Lazy()], "querying": Querying()})

    assert attrs == {
        "template.context.names": ["lazy", "inside", "querying"],
        "template.context.values": [
            "<lazy, not evaluated>",
            "[<lazy, not evaluated>]",
            "<Querying: not shown, it would query the database>",
        ],
    }

    assert entered == ["in", "out"]
