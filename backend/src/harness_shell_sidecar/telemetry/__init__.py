"""Local-only OpenTelemetry setup."""

from .local_exporter import LocalSpanExporter, build_local_tracer_provider

__all__ = ["LocalSpanExporter", "build_local_tracer_provider"]

