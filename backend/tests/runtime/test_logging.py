import io
import logging
import re
from contextlib import redirect_stdout

import pytest

from harness_shell_sidecar.telemetry import configure_stderr_logging


def _parts(line: str) -> list[str]:
    """Split one console record into the six configured columns."""

    return line.rstrip("\n").split(" | ", maxsplit=5)


def test_logging_emits_slf4j_style_columns_to_stderr_only() -> None:
    stderr = io.StringIO()
    stdout = io.StringIO()
    configure_stderr_logging(stderr)
    logger = logging.getLogger("harness_shell_sidecar.test")

    with redirect_stdout(stdout):
        logger.info("agent_run_started agent_run_id=%s", "run-123")

    timestamp, level, request_id, thread, logger_name, message = _parts(
        stderr.getvalue()
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", timestamp)
    assert level == "INFO "
    assert request_id == ""
    assert thread == "MainThread"
    assert logger_name == "harness_shell_sidecar.test"
    assert message == "agent_run_started agent_run_id=run-123"
    assert stdout.getvalue() == ""


def test_logging_preserves_direct_warning_message() -> None:
    marker = "ordinary-python-log-message"
    stderr = io.StringIO()
    configure_stderr_logging(stderr)

    logging.getLogger("third_party").warning("warning reason=%s", marker)

    _timestamp, level, _request_id, _thread, logger_name, message = _parts(
        stderr.getvalue()
    )
    assert level == "WARNING"
    assert logger_name == "third_party"
    assert message == f"warning reason={marker}"


def test_debug_logging_is_hidden_at_the_default_info_level() -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr)

    logging.getLogger("harness_shell_sidecar.test").debug("node detail")

    assert stderr.getvalue() == ""


def test_debug_logging_can_be_enabled_explicitly() -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr, level=logging.DEBUG)

    logging.getLogger("harness_shell_sidecar.test").debug(
        "agent_node_completed node=%s", "call_model"
    )

    _timestamp, level, _request_id, _thread, _logger_name, message = _parts(
        stderr.getvalue()
    )
    assert level == "DEBUG"
    assert message == "agent_node_completed node=call_model"


@pytest.mark.parametrize(
    ("level", "level_name", "level_color"),
    [
        (logging.DEBUG, "DEBUG", "\x1b[36m"),
        (logging.INFO, "INFO ", "\x1b[32m"),
        (logging.WARNING, "WARNING", "\x1b[33m"),
        (logging.ERROR, "ERROR", "\x1b[31m"),
    ],
)
def test_colorized_logging_uses_the_console_palette(
    level: int,
    level_name: str,
    level_color: str,
) -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr, level=logging.DEBUG, colorize=True)

    logging.getLogger("harness_shell_sidecar.test").log(level, "message")

    rendered = stderr.getvalue()
    assert rendered.startswith("\x1b[39m\x1b[2m")
    assert f"{level_color}{level_name}\x1b[0m\x1b[39m" in rendered
    assert "\x1b[36mMainThread\x1b[0m\x1b[39m" in rendered
    assert (
        "\x1b[33mharness_shell_sidecar.test\x1b[0m\x1b[39m"
        in rendered
    )
    assert rendered.endswith(" | message\x1b[0m\n")
