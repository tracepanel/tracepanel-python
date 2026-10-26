import platform

from dirty_equals import IsStr

from tracepanel.core import runtime

PYTHON = ".".join(platform.python_version().split(".")[:2])


def test_a_known_framework_with_its_version() -> None:
    runtime.set_framework("Django")

    assert runtime.label() == IsStr(regex=rf"Python {PYTHON} · Django \d+\.\d+")
    assert runtime.attrs() == {
        "telemetry.sdk.language": "python",
        "process.runtime.name": "cpython",
        "process.runtime.version": platform.python_version(),
        "tracepanel.framework": IsStr(regex=r"Django \d+\.\d+.*"),
    }


def test_a_generic_app_is_just_python() -> None:
    runtime.set_framework("ASGI app")

    assert runtime.label() == f"Python {PYTHON}"
    assert "tracepanel.framework" not in runtime.attrs()
