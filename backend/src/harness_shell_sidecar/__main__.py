"""Command-line entry point for the Harness Shell sidecar."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from harness_shell_sidecar.telemetry import configure_stderr_logging, log_event
from harness_shell_sidecar.web.server import desktop, serve


LOGGER = logging.getLogger("harness_shell_sidecar.process")


def _serve_port(value: str) -> int:
    """Parse one explicit non-privileged-or-privileged TCP port value."""

    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _desktop_port(value: str) -> int:
    """Accept only dynamic port selection for packaged desktop mode."""

    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if port != 0:
        raise argparse.ArgumentTypeError("desktop port must be 0")
    return port


def _absolute_path(value: str) -> Path:
    """Parse one explicit absolute path without environment expansion."""

    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("data directory must be absolute")
    return path


def _inherited_handle(value: str) -> int:
    """Parse one nonzero inherited Windows HANDLE numeric value."""

    try:
        handle = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("handle must be an integer") from error
    if handle <= 0:
        raise argparse.ArgumentTypeError("handle must be greater than zero")
    return handle


def build_parser() -> argparse.ArgumentParser:
    """Build the strict CLI with no legacy stdio or host override path."""

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
    """Run one autonomous loopback backend in development or desktop mode."""

    args = build_parser().parse_args(argv)
    configure_stderr_logging()
    log_event(LOGGER, logging.INFO, "sidecar_process_started")
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
