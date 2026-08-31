"""Local OpenTelemetry and safe structured logging interfaces."""

from .local_exporter import LocalSpanExporter, build_local_tracer_provider
from .logging import (
    JsonLogFormatter,
    SafeLogValue,
    configure_stderr_logging,
    extract_safe_exception_fields,
    log_event,
    log_exception_event,
)

__all__ = [
    "JsonLogFormatter",
    "LocalSpanExporter",
    "SafeLogValue",
    "build_local_tracer_provider",
    "configure_stderr_logging",
    "extract_safe_exception_fields",
    "log_event",
    "log_exception_event",
]
