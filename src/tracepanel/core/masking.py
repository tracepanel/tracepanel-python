"""Which names hold a secret, and where a payment card's number is: what every capture masks.

A name is read word by word, split at `_ - . [ ]` and the like and where camelCase turns: x-api-key, card[number],
cardNumber, APIKey and X-CSRFToken are (x, api, key), (card, number), (card, number), (api, key), (x, csrf, token).
A secret's word must be a whole word, so `author`, `authority` and `is_authenticated` aren't `auth`, and
`tokenizer` isn't `token`. Words written together are read too: csrftoken and accesstoken end in a token, sessionid
starts with a session, PHPSESSID ends in one. `tokens` is no secret's word: it counts a model's tokens
(max_tokens, prompt_tokens, total_tokens).

Two kinds of names hold no secret though a secret's word is in them: one that says something about the secret
(password_length, token_type, session_timeout, cookie_name, token_expires_at) and a yes or no (is_private,
has_password).

A card's number hides under a name of its own (card_number, pan) or under `number` in the card's object, which is
named after the card ({"card": {"number": ...}}) or holds the card's other fields: Stripe's
{"source": {"number": ..., "exp_month": 12, "exp_year": 2030, "cvc": ...}}. Under a number's name a card's digits are
masked whatever is beside them: see card_digits().
"""

import functools
import itertools
import re
from collections.abc import Iterable

MASK = "********"

_WORD_BREAKS = re.compile(r"[^a-zA-Z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# A card's number and its security code (CVC, CVV, CVN), for every rule.
_CARD_WORDS = frozenset({"pan", "cvc", "cvv", "cvn", "cvc2", "cvv2", "cardnumber", "cardnum", "cardno"})
_CARD_PAIRS = frozenset({("card", "number"), ("card", "num"), ("card", "no")})
# The words of a secret in a request, a local variable, a log record's extra or a template's context.
_WORDS = _CARD_WORDS | {
    "pass",
    "passwd",
    "password",
    "passwords",
    "passphrase",
    "passcode",
    "passkey",
    "secret",
    "secrets",
    "secretkey",
    "token",
    "apikey",
    "apikeys",
    "auth",
    "oauth",
    "oauth2",
    "authorization",
    "authentication",
    "cookie",
    "cookies",
    "session",
    "sessions",
    "csrf",
    "xsrf",
    "signature",
    "signatures",
    "private",
}
_PAIRS = _CARD_PAIRS | {("api", "key"), ("api", "keys")}
# Settings: Django's idea of a hidden setting. A session, a cookie, CSRF and auth say nothing secret as settings
# (SESSION_COOKIE_AGE, AUTH_USER_MODEL, CSRF_COOKIE_NAME), but any key does (SECRET_KEY, AWS_ACCESS_KEY_ID), and a
# credential (GOOGLE_CREDENTIALS).
_SETTING_WORDS = _CARD_WORDS | {
    "pass",
    "passwd",
    "password",
    "passwords",
    "passphrase",
    "passcode",
    "passkey",
    "secret",
    "secrets",
    "secretkey",
    "token",
    "apikey",
    "apikeys",
    "key",
    "keys",
    "privatekey",
    "signature",
    "signatures",
    "private",
    "credential",
    "credentials",
}
# Words written together: accesstoken, csrftoken, clientsecret, newpassword, xapikey, jsessionid, PHPSESSID...
_ENDS = ("token", "secret", "password", "passwd", "apikey", "sessionid", "sessid")
# ...and csrfmiddlewaretoken, sessionkey, cookiejar. Not `secret` (secretary), `token` (tokenizer) nor `auth` (author).
_STARTS = ("password", "apikey", "csrf", "xsrf", "session", "cookie")
_SETTING_STARTS = ("password", "apikey")
# The last word of a name about a secret, not the secret: password_length, token_type, cookie_name.
_ABOUT = frozenset(
    {
        "count",
        "total",
        "length",
        "len",
        "min",
        "max",
        "size",
        "age",
        "timeout",
        "ttl",
        "expiry",
        "expires",
        "expiration",
        "at",
        "type",
        "name",
        "field",
        "label",
        "required",
        "enabled",
    }
)
# The first word of a yes or no: is_private, has_password.
_YES_OR_NO = frozenset({"is", "has"})


@functools.lru_cache(maxsize=8192)
def words(name: str) -> tuple[str, ...]:
    """A name's words, lowercase: `X-CSRFToken` -> ("x", "csrf", "token"), `card[number]` -> ("card", "number")."""
    return tuple(word.lower() for word in _WORD_BREAKS.split(name) if word)


def _secret(name: str, secret_words: frozenset[str], starts: tuple[str, ...]) -> bool:
    found = words(name)
    if len(found) > 1 and (found[0] in _YES_OR_NO or found[-1] in _ABOUT):
        return False
    return any(word in secret_words or word.endswith(_ENDS) or word.startswith(starts) for word in found) or any(
        pair in _PAIRS for pair in itertools.pairwise(found)
    )


@functools.lru_cache(maxsize=4096)
def sensitive(name: str) -> bool:
    """Whether a secret goes under the name: password, x-api-key, Authorization, csrftoken, sessionid, card[cvc].

    The rule of a request (headers, cookies, query, bodies, the session), an error's local variables, a log record's
    extras and a template's context.
    """
    return _secret(name, _WORDS, _STARTS)


@functools.lru_cache(maxsize=4096)
def setting_sensitive(name: str) -> bool:
    """Whether a setting (or a key inside one) holds a secret by its name: SECRET_KEY, DB_PASS, AWS_TOKEN."""
    return _secret(name, _SETTING_WORDS, _SETTING_STARTS)


# A card's number in its object: under `number` (num, no).
_NUMBER = frozenset({("number",), ("num",), ("no",)})
# Names a card's number goes under, its digits checked: number, num, account.
_DIGITS_UNDER = frozenset({("number",), ("num",), ("account",)})
# What holds a payment card (card, creditCard, payment_card): its `number` is the card's number.
_CARD = re.compile(r"card$", re.IGNORECASE)
# A card's other fields, by their last words: its code, its expiry (exp_month, expiryYear, expiration_date).
_CARD_FIELDS = frozenset(
    {
        ("cvc",),
        ("cvv",),
        ("cvn",),
        ("cvc2",),
        ("cvv2",),
        ("expiry",),
        ("expiration",),
        *((first, last) for first in ("exp", "expiry", "expiration") for last in ("month", "year", "date")),
    }
)
_DIGITS = re.compile(r"\d[\d -]{11,}\d")
_CARD_LENGTHS = range(13, 20)


def card_fields(keys: Iterable[object]) -> bool:
    """Whether an object's keys have a card's other fields beside its number: cvc, exp_month, expiry."""
    for key in keys:
        if isinstance(key, str):
            found = words(key)
            if found[-1:] in _CARD_FIELDS or found[-2:] in _CARD_FIELDS:
                return True
    return False


def card_number(key: object, value: object, parent: object = None, *, beside_card: bool = False) -> bool:
    """Whether `value` under `key` is a card's number, by where it is or what it is.

    Where: `number` under a card ({"card": {"number": ...}}), or beside the card's other fields (`beside_card`, as
    card_fields() says of its object). What: card_digits() under a number's name.
    """
    if not isinstance(key, str):
        return False
    found = words(key)
    if found in _NUMBER and ((isinstance(parent, str) and _CARD.search(parent) is not None) or beside_card):
        return True
    return found in _DIGITS_UNDER and card_digits(value)


def card_digits(value: object) -> bool:
    """Whether a value reads as a card's number: 13 to 19 digits, the first 2 to 6, and the Luhn check passes.

    Spaces and dashes may group the digits, and the first is a card network's (Visa 4, Mastercard 2 and 5, Amex 3, ...).
    Checked only under a number's name (number, num, account), never an id's: a snowflake id (17 to 19 digits) lives
    under `id`. Any 13 to 19 digits pass the Luhn check by chance one time in ten, so one order number in ten or so
    under `number` is masked too: a number hidden on a developer's own machine costs less than a card shown.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    text = str(value).strip()
    if not _DIGITS.fullmatch(text):
        return False
    digits = text.replace(" ", "").replace("-", "")
    return len(digits) in _CARD_LENGTHS and digits[0] in "23456" and _luhn(digits)


def _luhn(digits: str) -> bool:
    total = 0
    for place, digit in enumerate(int(each) for each in reversed(digits)):
        total += sum(divmod(digit * 2 if place % 2 else digit, 10))
    return total % 10 == 0
