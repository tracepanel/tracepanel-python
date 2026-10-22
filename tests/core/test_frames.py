"""Which frames are the project's: the place Tracepanel names for a query is the first of them."""

import sysconfig
from pathlib import Path

import pytest

from tracepanel.core import frames


def test_a_script_of_the_environment_is_not_the_project_s() -> None:
    # A server started as `uvicorn` (or `gunicorn`, `celery`) has the environment's script at the bottom of its stack:
    # .venv/bin/uvicorn, inside the project's directory.
    script = Path(sysconfig.get_paths()["scripts"]) / "uvicorn"

    assert frames.is_in_app(str(script), "__main__") is False


def test_a_file_of_the_project_is() -> None:
    assert frames.is_in_app(str(Path("shop/schemas.py").resolve()), "shop.schemas") is True


def test_a_local_holding_a_cards_number_or_code_is_masked() -> None:
    shown = {name: frames.safe_repr(name, "4242424242424242") for name in ("card_number", "cvv", "pan", "company")}

    assert shown == {
        "card_number": frames.MASK,
        "cvv": frames.MASK,
        "pan": frames.MASK,
        "company": "'4242424242424242'",
    }


def test_a_locals_card_number_is_masked_beside_its_other_fields_or_by_its_digits() -> None:
    def charge(number: str, exp_month: int, cvc: str, author: str) -> None:
        raise ValueError(number, exp_month, cvc, author)

    def refund(number: str, order_number: str) -> None:
        raise ValueError(number, order_number)

    shown = []
    for call in (lambda: charge("1", 12, "123", "ann"), lambda: refund("4242424242424242", "17")):
        try:
            call()
        except ValueError as error:
            shown.append(frames.from_traceback(error.__traceback__, with_locals=True)[-1].locals)

    assert shown == [
        {"number": frames.MASK, "exp_month": "12", "cvc": frames.MASK, "author": "'ann'"},
        {"number": frames.MASK, "order_number": "'17'"},
    ]


def test_a_locals_tokens_are_masked_unless_they_are_a_models_counts() -> None:
    def complete(tokens: list[int], refresh_tokens: list[int], max_tokens: int) -> None:
        raise ValueError(tokens, refresh_tokens, max_tokens)

    with pytest.raises(ValueError, match="1024") as raised:
        complete([12, 7], [1], 1024)
    shown = frames.from_traceback(raised.value.__traceback__, with_locals=True)[-1].locals

    assert shown == {"tokens": "[12, 7]", "refresh_tokens": frames.MASK, "max_tokens": "1024"}
