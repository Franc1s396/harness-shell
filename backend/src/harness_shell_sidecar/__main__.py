"""Command-line entry point for the Harness Shell sidecar."""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

from harness_shell_sidecar.runtime import SidecarService
from harness_shell_sidecar.runtime.windows_job import attach_required_job


class JsonLogFormatter(logging.Formatter):
    """将 Sidecar 日志格式化为写入 stderr 的单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """把一条标准日志记录转换为稳定、可机器解析的 JSON 文本。"""

        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        )
        payload = {
            "timestamp": timestamp.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_stderr_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main() -> int:
    if getattr(sys, "frozen", False):
        attach_required_job()
    configure_stderr_logging()
    try:
        return asyncio.run(SidecarService.for_stdio().run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
