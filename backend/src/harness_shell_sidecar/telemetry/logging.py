"""配置带请求关联的人类可读 stderr 日志。"""

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
    """返回标准日志级别对应的 ANSI 前景色。"""

    if level >= logging.ERROR:
        return _ANSI_RED
    if level >= logging.WARNING:
        return _ANSI_YELLOW
    if level >= logging.INFO:
        return _ANSI_GREEN
    return _ANSI_CYAN


class ConsoleLogFormatter(logging.Formatter):
    """渲染 SLF4J 风格的控制台行，不过滤消息内容。"""

    # 仅为显式选择的交互式控制台启用 ANSI 序列。
    _colorize: bool

    def __init__(self, *, colorize: bool = False) -> None:
        """创建可选输出 ANSI 控制台颜色的格式器。"""

        super().__init__()
        self._colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        """返回时间戳、级别、请求、线程、logger 和消息列。"""

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
    """安装进程级 stderr logger，可选启用 ANSI 颜色。"""

    target = stream if stream is not None else sys.stderr
    if stream is None and hasattr(target, "reconfigure"):
        target.reconfigure(encoding="utf-8", errors="strict")
    handler = logging.StreamHandler(target)
    handler.setFormatter(ConsoleLogFormatter(colorize=colorize))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def bind_request_id(request_id: UUID) -> Token[str]:
    """将已校验 HTTP 请求 ID 绑定到当前异步上下文。"""

    return _CURRENT_REQUEST_ID.set(str(request_id))


def reset_request_id(token: Token[str]) -> None:
    """恢复外层上下文拥有的请求关联值。"""

    _CURRENT_REQUEST_ID.reset(token)
