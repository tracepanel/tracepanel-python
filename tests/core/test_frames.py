"""Which frames are the project's: the place Tracepanel names for a query is the first of them."""

import sysconfig
from pathlib import Path

import pytest

from tracepanel.core import conf, frames


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


@pytest.fixture
def linked_python(tmp_path: Path) -> tuple[Path, Path]:
    """A Python installed as uv installs one: its directory a symlink (cpython-3.14-… to cpython-3.14.0-…)."""
    real = tmp_path / "cpython-3.14.0-macos" / "lib" / "python3.14"
    real.mkdir(parents=True)
    (real / "threading.py").write_text("")
    (tmp_path / "cpython-3.14-macos").symlink_to(tmp_path / "cpython-3.14.0-macos")
    return tmp_path / "cpython-3.14-macos" / "lib" / "python3.14", real


@pytest.mark.parametrize("roots", ["link", "real"])
def test_a_library_frame_is_named_by_its_package_path_through_a_symlinked_python(
    linked_python: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, roots: str
) -> None:
    link, real = linked_python
    monkeypatch.setattr(frames, "_LIBRARY_PATHS", frames._library_roots([str(link if roots == "link" else real)]))  # noqa: SLF001
    frames.library_path.cache_clear()

    named = {frames.library_path(str(stdlib / "threading.py")) for stdlib in (link, real)}

    assert named == {"threading.py"}


def test_a_module_loaded_through_a_symlinked_python_is_not_the_project_s(
    linked_python: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    link, real = linked_python
    monkeypatch.setattr(frames, "_LIBRARY_PATHS", frames._library_roots([str(real)]))  # noqa: SLF001
    frames.is_in_app.cache_clear()
    frames.library_path.cache_clear()
    code = compile("", str(link / "threading.py"), "exec")

    assert (frames.is_in_app(code.co_filename, "threading"), frames.library_path(code.co_filename)) == (
        False,
        "threading.py",
    )


@pytest.mark.parametrize("root", ["link", "real"])
def test_a_project_file_is_named_relative_to_a_symlinked_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    # /tmp is /private/tmp on macOS: the root and a code object's file may name the project either way.
    (tmp_path / "real" / "shop").mkdir(parents=True)
    (tmp_path / "real" / "shop" / "views.py").write_text("")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.setenv("TRACEPANEL_ROOT", str(tmp_path / root))
    conf.reset()

    named = {frames.relative(str(tmp_path / project / "shop" / "views.py")) for project in ("link", "real")}

    assert named == {str(Path("shop", "views.py"))}
