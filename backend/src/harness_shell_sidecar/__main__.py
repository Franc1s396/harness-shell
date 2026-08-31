"""Command-line entry point for the Harness Shell sidecar."""

import asyncio
import logging
import sys

from harness_shell_sidecar.runtime import SidecarService
from harness_shell_sidecar.runtime.windows_job import attach_required_job
from harness_shell_sidecar.telemetry import configure_stderr_logging, log_event


LOGGER = logging.getLogger("harness_shell_sidecar.process")


def main() -> int:
    """Configure logging, attach packaged process ownership, and run stdio."""

    if getattr(sys, "frozen", False):
        attach_required_job()
    configure_stderr_logging()
    log_event(LOGGER, logging.INFO, "sidecar_process_started")
    try:
        return asyncio.run(SidecarService.for_stdio().run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
