"""Configure human-readable stderr logging with request correlation."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar, Token
from typing import TextIO
from uuid import UUID


_CURRENT_REQUEST_ID: ContextVar[str] = ContextVar(
    "harness_shell_request_id",
    default="",
)
_ANSI_RESET = "\x1b[0m"
_ANSI_DEFAULT_FOREGROUND = "\x1b[39m"
_ANSI_FAINT = "\x1b[2m"
_ANSI_RED = "\x1b[31m"
_ANSI_GREEN = "\x1b[32m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_CYAN = "\x1b[36m"


def _level_color(level: int) -> str:
    """Return the ANSI foreground color for one standard logging level."""

    if level >= logging.ERROR:
        return _ANSI_RED
    if level >= logging.WARNING:
        return _ANSI_YELLOW
    if level >= logging.INFO:
        return _ANSI_GREEN
    return _ANSI_CYAN


class ConsoleLogFormatter(logging.Formatter):
    """Render one SLF4J-style console line without filtering its message."""

    # Enables ANSI sequences only for explicitly selected interactive consoles.
    _colorize: bool

    def __init__(self, *, colorize: bool = False) -> None:
        """Create a formatter that optionally emits ANSI console colors."""

        super().__init__()
        self._colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        """Return timestamp, level, request, thread, logger, and message columns."""

        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        timestamp = f"{timestamp}.{int(record.msecs):03d}"
        request_id = _CURRENT_REQUEST_ID.get()
        if self._colorize:
            level = f"{_level_color(record.levelno)}{record.levelname:<5}"
            timestamp = f"{_ANSI_FAINT}{timestamp}"
            request_id = f"{_ANSI_FAINT}{request_id}"
            thread = f"{_ANSI_CYAN}{record.threadName}"
            logger_name = f"{_ANSI_YELLOW}{record.name}"
            logger_message = f"{_ANSI_FAINT}{record.getMessage()}"
            separator = f"{_ANSI_RESET}{_ANSI_DEFAULT_FOREGROUND} | "
            rendered = (
                f"{_ANSI_DEFAULT_FOREGROUND}{timestamp}{separator}"
                f"{level}{separator}{request_id} | {thread}{separator}"
                f"{logger_name}{separator}{logger_message}"
            )
        else:
            rendered = (
                f"{timestamp} | {record.levelname:<5} | {request_id} | "
                f"{record.threadName} | {record.name} | {record.getMessage()}"
            )
        if record.exc_info is not None:
            rendered = f"{rendered}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            rendered = f"{rendered}\n{self.formatStack(record.stack_info)}"
        if self._colorize:
            rendered = f"{rendered}{_ANSI_RESET}"
        return rendered


def configure_stderr_logging(
    stream: TextIO | None = None,
    *,
    level: int = logging.INFO,
    colorize: bool = False,
) -> None:
    """Install the process-wide stderr logger with optional ANSI colors."""

    target = stream if stream is not None else sys.stderr
    if stream is None and hasattr(target, "reconfigure"):
        target.reconfigure(encoding="utf-8", errors="strict")
    handler = logging.StreamHandler(target)
    handler.setFormatter(ConsoleLogFormatter(colorize=colorize))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def bind_request_id(request_id: UUID) -> Token[str]:
    """Bind one validated HTTP request ID to the current async context."""

    return _CURRENT_REQUEST_ID.set(str(request_id))


def reset_request_id(token: Token[str]) -> None:
    """Restore the request correlation value owned by the outer context."""

    _CURRENT_REQUEST_ID.reset(token)
