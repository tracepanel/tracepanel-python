import os
import subprocess
import sys
from pathlib import Path

import pytest
from dirty_equals import IsPositiveInt, IsStr

devtools = pytest.importorskip("devtools")

from tracepanel.core.context import start_span, start_trace  # noqa: E402 (optional test group)
from tracepanel.integrations import devtools as integration  # noqa: E402

integration.install()
debug = devtools.debug
HERE = "tests/integrations/test_devtools.py"
ID = IsStr(regex=r"[0-9A-HJKMNP-TV-Z]{26}")


def test_a_debug_call_is_a_dump_record_of_the_span_it_was_called_in(capsys: pytest.CaptureFixture[str]) -> None:
    items = [1, 2, 3]

    with start_trace("http", "GET /orders") as trace, start_span("function", "view") as span:
        line = sys._getframe().f_lineno + 1  # noqa: SLF001
        returned = debug(items, total=len(items))

    assert returned == ([1, 2, 3], {"total": 3})

    # The terminal gets what devtools prints without tracepanel.
    printed = (
        f"{HERE}:{line} test_a_debug_call_is_a_dump_record_of_the_span_it_was_called_in\n"
        "    items: [1, 2, 3] (list) len=3\n"
        "    total: 3 (int)\n"
    )
    assert capsys.readouterr().out == printed

    assert span is not None
    assert [log.to_dict() for log in trace.logs] == [
        {
            "span_id": span.id,
            "time": IsPositiveInt,
            "level": "debug",
            "logger": "devtools",
            "message": printed.removesuffix("\n"),
            "exception": None,
            "attrs": {
                "log.kind": "dump",
                "dump.args": [
                    {"expr": "items", "repr": "[1, 2, 3]", "type": "list", "len": 3},
                    {"expr": "total", "repr": "3", "type": "int", "len": None},
                ],
                "code.origin": f"{HERE}:{line} in test_a_debug_call_is_a_dump_record_of_the_span_it_was_called_in",
            },
        }
    ]


def test_a_secret_is_masked_by_its_expression_keyword_or_variable(capsys: pytest.CaptureFixture[str]) -> None:
    user_password = "hunter2"
    token = "tok-1"

    with start_trace("http", "GET /login") as trace:
        line = sys._getframe().f_lineno + 1  # noqa: SLF001
        debug(user_password, "a literal", api_key="sk-1", value=token)

    assert "hunter2" in capsys.readouterr().out

    [log] = trace.logs
    assert log.attrs["dump.args"] == [
        {"expr": "user_password", "repr": "********", "type": "str", "len": None},
        {"expr": None, "repr": "'a literal'", "type": "str", "len": 9},
        {"expr": "api_key", "repr": "********", "type": "str", "len": None},
        {"expr": "value", "repr": "********", "type": "str", "len": None},
    ]

    # devtools' text, the secrets' values (and their lengths) masked.
    assert log.message == (
        f"{HERE}:{line} test_a_secret_is_masked_by_its_expression_keyword_or_variable\n"
        "    user_password: ******** (str)\n"
        "    'a literal' (str) len=9\n"
        "    api_key: ******** (str)\n"
        "    value: ******** (str)"
    )


def test_a_long_value_is_cut_and_marked() -> None:
    with start_trace("http", "GET /") as trace:
        debug(text="x" * 20_000, file_=open(os.devnull, "w"))  # noqa: PTH123, SIM115 (closed with the process)

    # devtools splits a long string over lines.
    [entry] = trace.logs[0].attrs["dump.args"]  # type: ignore[misc]
    assert entry == {
        "expr": "text",
        "repr": IsStr(regex=r"\(\n(    'x+'\n)+    'x+… \(truncated\)", max_length=integration.MAX_REPR + 20),
        "type": "str",
        "len": 20_000,
    }


def test_outside_a_trace_nothing_is_recorded_and_devtools_prints_as_ever(capsys: pytest.CaptureFixture[str]) -> None:
    with start_trace("http", "GET /") as trace:
        pass

    returned = debug(42)

    assert returned == 42
    assert capsys.readouterr().out.endswith("\n    42 (int)\n")

    assert trace.logs == []


def test_format_is_not_a_dump_and_a_timer_is_a_function_span(capsys: pytest.CaptureFixture[str]) -> None:
    with start_trace("http", "GET /") as trace:
        output = debug.format(1)
        with debug.timer("load orders"):
            pass

    assert str(output).endswith("\n    1 (int)")
    assert capsys.readouterr().out == IsStr(regex=r"load orders: \d+\.\d{3}s elapsed\n")

    assert trace.logs == []

    assert [(span.kind, span.name, span.parent_id) for span in trace.spans[1:]] == [
        ("function", "load orders", trace.spans[0].id)
    ]


SCRIPT = """
from tracepanel.core import registry
from tracepanel.core.context import start_trace

registry.install()
from devtools import debug

with start_trace("http", "GET /") as trace:
    debug(1)
print(len(trace.logs))
"""


@pytest.mark.parametrize(("setting", "logs"), [("1", "1"), ("0", "0")])
def test_the_setting_turns_it_off(tmp_path: Path, setting: str, logs: str) -> None:
    env = {**os.environ, "TRACEPANEL_AGENT": "off", "TRACEPANEL_DEVTOOLS_ENABLED": setting}
    done = subprocess.run(  # noqa: S603 (this interpreter)
        [sys.executable, "-c", SCRIPT], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
    )

    assert done.stdout.splitlines()[-1] == logs
