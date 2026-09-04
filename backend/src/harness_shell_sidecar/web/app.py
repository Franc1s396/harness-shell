"""Import-side-effect-free FastAPI application factory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harness_shell_sidecar.runtime.settings import RuntimeSettings

from .access_logging import HttpAccessLogMiddleware
from .errors import register_exception_handlers
from .lifespan import ResourceFactory, default_resource_factory, runtime_lifespan
from .limits import BodyLimitMiddleware
from .websocket import runtime_websocket_endpoint
from .routes import (
    agent_router,
    connections_router,
    diagnostics_router,
    host_keys_router,
    manual_sftp_router,
    runtime_router,
    ssh_router,
    terminal_router,
)


def create_app(
    *,
    settings: RuntimeSettings,
    resource_factory: ResourceFactory | None = None,
    log_directory_opener: Callable[[Path], object] | None = None,
) -> FastAPI:
    """Build the private ASGI app without opening DBs or starting background work."""

    app = FastAPI(
        title="Harness Shell Private Python Runtime API",
        version="1.0.0",
        description=(
            "Loopback-only API owned by the Python process. "
            "The Runtime WebSocket is specified separately."
        ),
        servers=[
            {
                "url": "http://127.0.0.1:{port}",
                "variables": {"port": {"default": "8765"}},
            }
        ],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=runtime_lifespan,
    )
    app.state.resource_factory = resource_factory or default_resource_factory
    app.state.settings = settings
    from .routes.diagnostics import open_log_directory_with_explorer

    app.state.log_directory_opener = (
        log_directory_opener or open_log_directory_with_explorer
    )
    register_exception_handlers(app)
    app.include_router(runtime_router)
    app.include_router(connections_router)
    app.include_router(host_keys_router)
    app.include_router(ssh_router)
    app.include_router(terminal_router)
    app.include_router(agent_router)
    app.include_router(manual_sftp_router)
    app.include_router(diagnostics_router)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://tauri.localhost", "http://localhost:1420"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
        allow_headers=["Content-Type", "X-Request-ID", "X-Chunk-Offset"],
        expose_headers=[
            "X-Request-ID",
            "X-Chunk-Sequence",
            "X-Chunk-Offset",
            "X-Chunk-Byte-Count",
            "X-Chunk-EOF",
        ],
    )
    # Register last so access logging owns the complete HTTP middleware chain.
    app.add_middleware(HttpAccessLogMiddleware)
    app.add_api_websocket_route(
        "/v1/runtime/events",
        runtime_websocket_endpoint,
        name="runtime_events",
    )
    return app
