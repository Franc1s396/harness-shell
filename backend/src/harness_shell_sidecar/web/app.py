"""Import-side-effect-free FastAPI application factory."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from .errors import register_exception_handlers
from .lifespan import (
    ResourceFactory,
    default_resource_factory,
    runtime_lifespan,
)
from .limits import BodyLimitMiddleware
from .websocket import runtime_websocket_endpoint
from .routes import (
    agent_router,
    connections_router,
    host_keys_router,
    manual_sftp_router,
    runtime_router,
    ssh_router,
    terminal_router,
)


def create_app(
    *,
    resource_factory: ResourceFactory | None = None,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the private ASGI app without opening DBs or starting background work."""

    app = FastAPI(
        title="Harness Shell Private Python Runtime API",
        version="1.0.0",
        description=(
            "Loopback-only API owned by the Rust-managed packaged child. "
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
    app.state.shutdown_callback = shutdown_callback or (lambda: None)
    register_exception_handlers(app)
    app.include_router(runtime_router)
    app.include_router(connections_router)
    app.include_router(host_keys_router)
    app.include_router(ssh_router)
    app.include_router(terminal_router)
    app.include_router(agent_router)
    app.include_router(manual_sftp_router)
    app.add_middleware(BodyLimitMiddleware)
    app.add_api_websocket_route(
        "/v1/runtime/events",
        runtime_websocket_endpoint,
        name="runtime_events",
    )
    return app
