"""Harness Shell Sidecar 的命令行入口。"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from harness_shell_sidecar.telemetry import configure_stderr_logging
from harness_shell_sidecar.web.server import desktop, serve


LOGGER = logging.getLogger("harness_shell_sidecar.process")


def _serve_port(value: str) -> int:
    """解析显式指定的 TCP 端口，允许特权端口与非特权端口。"""

    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _desktop_port(value: str) -> int:
    """打包版桌面模式只接受动态端口分配。"""

    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if port != 0:
        raise argparse.ArgumentTypeError("desktop port must be 0")
    return port


def _absolute_path(value: str) -> Path:
    """解析显式绝对路径，不展开环境变量。"""

    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("data directory must be absolute")
    return path


def _inherited_handle(value: str) -> int:
    """解析非零的继承 Windows HANDLE 数值。"""

    try:
        handle = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("handle must be an integer") from error
    if handle <= 0:
        raise argparse.ArgumentTypeError("handle must be greater than zero")
    return handle


def build_parser() -> argparse.ArgumentParser:
    """构建严格命令行入口，不保留旧 stdio 或 host 覆盖路径。"""

    parser = argparse.ArgumentParser(
        prog="harness-shell-sidecar",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", allow_abbrev=False)
    serve_parser.add_argument("--port", required=True, type=_serve_port)
    serve_parser.add_argument("--data-dir", required=True, type=_absolute_path)

    desktop_parser = commands.add_parser("desktop", allow_abbrev=False)
    desktop_parser.add_argument("--port", required=True, type=_desktop_port)
    desktop_parser.add_argument("--data-dir", required=True, type=_absolute_path)
    desktop_parser.add_argument(
        "--control-read-handle",
        required=True,
        type=_inherited_handle,
    )
    desktop_parser.add_argument(
        "--ready-write-handle",
        required=True,
        type=_inherited_handle,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """以开发或桌面模式运行自主初始化的 loopback 后端。"""

    args = build_parser().parse_args(argv)
    configure_stderr_logging(colorize=args.command == "serve")
    LOGGER.info("sidecar_process_started")
    try:
        if args.command == "serve":
            return serve(port=args.port, data_dir=args.data_dir)
        return desktop(
            port=args.port,
            data_dir=args.data_dir,
            control_read_handle=args.control_read_handle,
            ready_write_handle=args.ready_write_handle,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
