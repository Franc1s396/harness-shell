"""Command-line entry point for the Harness Shell sidecar."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from harness_shell_sidecar.runtime.windows_job import attach_required_job
from harness_shell_sidecar.telemetry import configure_stderr_logging, log_event
from harness_shell_sidecar.web.server import serve


LOGGER = logging.getLogger("harness_shell_sidecar.process")


def _port(value: str) -> int:
    """Parse one explicit non-privileged-or-privileged TCP port value."""

    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    """Build the strict CLI with no legacy stdio or host override path."""

    parser = argparse.ArgumentParser(
        prog="harness-shell-sidecar",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", allow_abbrev=False)
    serve_parser.add_argument("--port", required=True, type=_port)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Rust-owned loopback HTTP backend with explicit CLI arguments."""

    args = build_parser().parse_args(argv)
    if getattr(sys, "frozen", False):
        attach_required_job()
    configure_stderr_logging()
    log_event(LOGGER, logging.INFO, "sidecar_process_started")
    try:
        return serve(port=args.port)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
