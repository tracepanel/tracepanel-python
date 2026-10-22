"""Which names hold a secret, word by word, and where a payment card's number is."""

import pytest

from tracepanel.core import masking

# Names a request, a local variable, a log record's extra and a template's context mask.
SECRET = [
    # Passwords.
    "password",
    "passwd",
    "db_pass",
    "passphrase",
    "passcode",
    "passkey",
    "new_password",
    "password1",
    "password_confirm",
    "passwordConfirm",
    "passwords",
    "password_hash",
    # Secrets.
    "secret",
    "secrets",
    "client_secret",
    "clientSecret",
    "clientsecret",
    "webhook_secret",
    "secret_key",
    "secretKey",
    # Tokens.
    "token",
    "access_token",
    "accessToken",
    "accesstoken",
    "refresh_token",
    "id_token",
    "csrftoken",
    "csrf_token",
    "X-CSRFToken",
    "csrfmiddlewaretoken",
    "X-XSRF-TOKEN",
    "authenticity_token",
    "x-amz-security-token",
    # API keys.
    "api_key",
    "apiKey",
    "APIKey",
    "apikey",
    "x-api-key",
    "api_keys",
    # Auth: the header, its schemes, OAuth's.
    "auth",
    "x-auth",
    "X-Auth-Token",
    "basic_auth",
    "HTTPBasicAuth",
    "Authorization",
    "Proxy-Authorization",
    "authorization_code",
    "authentication",
    "oauth",
    "oauth2",
    "oauth_token",
    "oauth_secret",
    "OAuthToken",
    "_auth_user_hash",
    # Cookies and sessions.
    "cookie",
    "cookies",
    "Set-Cookie",
    "cookiejar",
    "session",
    "sessions",
    "sessionid",
    "sessionId",
    "session_id",
    "session_key",
    "PHPSESSID",
    "JSESSIONID",
    # CSRF, signatures, private keys.
    "csrf",
    "xsrf",
    "signature",
    "X-Hub-Signature-256",
    "Stripe-Signature",
    "private",
    "private_key",
    "privateKey",
    # Payment cards.
    "card_number",
    "cardNumber",
    "cardnumber",
    "card_no",
    "card_num",
    "card-number",
    "card[number]",
    "credit_card_number",
    "card_number_last4",
    "pan",
    "card_pan",
    "cvc",
    "cvv",
    "cvv2",
    "cvn",
    "card_cvc",
    "card[cvc]",
    "cvc_token",
]

# Names that only look like one: a secret's word inside another word, a name about a secret, a yes or no.
PLAIN = [
    # Not auth.
    "author",
    "authors",
    "author_id",
    "authorId",
    "authored_by",
    "authorship",
    "authority",
    "authorities",
    "authenticated",
    "is_authenticated",
    "authenticator",
    "authorized",
    "WWW-Authenticate",
    # Not a token: a model's tokens are counted.
    "tokenizer",
    "tokens",
    "max_tokens",
    "prompt_tokens",
    "total_tokens",
    # Not a password, a secret or a card.
    "bypass",
    "compass",
    "passenger",
    "passport",
    "secretary",
    "company",
    "span",
    "expand",
    "pane",
    "card_brand",
    "card_last4",
    "discount",
    "number",
    "account",
    "key",
    # About a secret.
    "passwords_count",
    "password_length",
    "token_type",
    "token_expires_at",
    "session_timeout",
    "cookie_name",
    # Yes or no.
    "has_password",
    "is_private",
]


@pytest.mark.parametrize("name", SECRET)
def test_a_name_with_a_secrets_word_is_masked(name: str) -> None:
    assert masking.sensitive(name) is True


@pytest.mark.parametrize("name", PLAIN)
def test_a_name_without_one_is_shown(name: str) -> None:
    assert masking.sensitive(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "SECRET_KEY",
        "DJANGO_SECRET_KEY",
        "STRIPE_SECRET",
        "AWS_ACCESS_KEY_ID",
        "FIELD_ENCRYPTION_KEY",
        "SOCIAL_AUTH_GITHUB_KEY",
        "EMAIL_HOST_PASSWORD",
        "REDIS_PASS",
        "GITHUB_TOKEN",
        "GOOGLE_CREDENTIALS",
        "JWT_PRIVATE_PEM",
        "SIGNING_SIGNATURE",
        "TEST_CARD_NUMBER",
        "STRIPE_TEST_CVC",
    ],
)
def test_a_setting_with_a_secrets_word_is_masked(name: str) -> None:
    assert masking.setting_sensitive(name) is True


@pytest.mark.parametrize(
    "name",
    [
        # What a request's rule masks says nothing secret as a setting.
        "SESSION_COOKIE_AGE",
        "SESSION_ENGINE",
        "CSRF_COOKIE_NAME",
        "AUTH_USER_MODEL",
        # A secret's word inside another word.
        "MONKEY_PATCH",
        "KEYCLOAK_REALM",
        "COMPASS_ROOT",
        "BYPASS_CACHE",
        "PASSPORT_URL",
        # About a secret.
        "PASSWORD_RESET_TIMEOUT",
        "TOKEN_EXPIRY",
    ],
)
def test_a_setting_without_one_is_shown(name: str) -> None:
    assert masking.setting_sensitive(name) is False


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("X-CSRFToken", ("x", "csrf", "token")),
        ("card[number]", ("card", "number")),
        ("creditCardNumber", ("credit", "card", "number")),
        ("APIKey", ("api", "key")),
        ("source.exp_month", ("source", "exp", "month")),
        ("oauth2", ("oauth2",)),
        ("__Host-Lang", ("host", "lang")),
    ],
)
def test_a_name_is_read_word_by_word(name: str, words: tuple[str, ...]) -> None:
    assert masking.words(name) == words


@pytest.mark.parametrize(
    "value",
    [
        "4242424242424242",
        "4242 4242 4242 4242",
        "4242-4242-4242-4242",
        4242424242424242,
        "5555555555554444",
        "2223003122003222",
        "378282246310005",
        "6011111111111117",
        "3056930009020004",
        "4222222222222",
        "6200000000000005",
        "6250941006528599",
    ],
)
def test_a_cards_digits_are_found(value: object) -> None:
    assert masking.card_digits(value) is True


@pytest.mark.parametrize(
    "value",
    [
        # The Luhn check fails.
        "4242424242424241",
        # A snowflake id: it starts with 1 (and lives under `id`, where nothing is checked).
        "1234567890123456789",
        # Too short, too long, not only digits.
        "424242424242",
        "42424242424242424242",
        "4242424242424242a",
        "+4242424242424242",
        "42424242.42424242",
        # Not a number.
        True,
        4.2,
        None,
    ],
)
def test_other_digits_are_not(value: object) -> None:
    assert masking.card_digits(value) is False


def test_a_cards_number_by_where_it_is_and_what_it_is() -> None:
    found = {
        "under its card": masking.card_number("number", "1", "creditCard"),
        "beside its fields": masking.card_number("num", "1", "source", beside_card=True),
        "its digits under a number's name": masking.card_number("account", "4242424242424242"),
        "other digits": masking.card_number("number", "4242424242424241"),
        "under another object": masking.card_number("number", "1", "order"),
        "its digits under another name": masking.card_number("phone", "4242424242424242"),
        "not a name": masking.card_number(7, "4242424242424242"),
    }

    assert found == {
        "under its card": True,
        "beside its fields": True,
        "its digits under a number's name": True,
        "other digits": False,
        "under another object": False,
        "its digits under another name": False,
        "not a name": False,
    }


@pytest.mark.parametrize(
    ("keys", "found"),
    [
        (["number", "cvc"], True),
        (["number", "exp_month"], True),
        (["number", "expYear"], True),
        (["number", "expiry"], True),
        (["number", "expiration_date"], True),
        (["number", "card_cvv"], True),
        (["number", "exp"], False),
        (["number", "expires_at"], False),
        (["number", "total", 7], False),
        ([], False),
    ],
)
def test_a_cards_other_fields_are_its_code_and_expiry(keys: list[object], found: bool) -> None:  # noqa: FBT001
    assert masking.card_fields(keys) is found
