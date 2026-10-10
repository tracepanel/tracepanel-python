from collections.abc import Generator

import pytest

from tracepanel.core import patching


class Base:
    def greet(self) -> str:
        return "base"


class Child(Base):
    def own(self) -> str:
        return "own"


def replaced(self: object) -> str:
    del self
    return "patched"


def empty_json(*args: object, **kwargs: object) -> str:
    del args, kwargs
    return "{}"


@pytest.fixture
def isolated() -> Generator[None, None, None]:
    # The registry is process-wide: keep the real patches (Django's, installed for the suite) out of this test.
    saved = dict(patching._applied)  # noqa: SLF001
    patching._applied.clear()  # noqa: SLF001
    yield
    patching._applied.clear()  # noqa: SLF001
    patching._applied.update(saved)  # noqa: SLF001


def test_patch_once_and_undo(isolated: None) -> None:
    assert patching.patch(Child, "own", replaced)
    assert not patching.patch(Child, "own", replaced)
    assert (Child().own(), patching.patched(Child, "own")) == ("patched", True)

    patching.undo()

    assert (Child().own(), patching.patched(Child, "own")) == ("own", False)


def test_inherited_attributes_come_back_inherited(isolated: None) -> None:
    patching.patch(Child, "greet", replaced)

    assert Child().greet() == "patched"
    assert patching.original(Child, "greet") is Base.__dict__["greet"]

    patching.undo()

    assert ("greet" in vars(Child), Child().greet()) == (False, "base")


def test_modules_too(isolated: None) -> None:
    import json  # noqa: PLC0415

    original = json.dumps
    patching.patch(json, "dumps", empty_json)

    assert ([patch.name for patch in patching.applied()], json.dumps({"a": 1})) == (["dumps"], "{}")

    patching.undo()

    assert json.dumps is original
