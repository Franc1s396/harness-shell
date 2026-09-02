"""Fixed loopback Uvicorn process entry for the private Python backend."""

from __future__ import annotations

import logging
import socket

import uvicorn

from harness_shell_sidecar.telemetry import log_event

from .app import create_app
from .websocket import MAX_WEBSOCKET_TEXT_BYTES, WEBSOCKET_QUEUE_CAPACITY


LOGGER = logging.getLogger("harness_shell_sidecar.web.server")
LOOPBACK_HOST = "127.0.0.1"


class _LoopbackServer(uvicorn.Server):
    """Log readiness only after Uvicorn has successfully bound the exact port."""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Bind first, then publish the allowlisted host and port."""

        await super().startup(sockets=sockets)
        if self.started:
            log_event(
                LOGGER,
                logging.INFO,
                "http_server_listening",
                host=LOOPBACK_HOST,
                port=self.config.port,
            )


def build_config(*, port: int, app=None) -> uvicorn.Config:
    """Build the only accepted private-loopback Uvicorn configuration."""

    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    return uvicorn.Config(
        app or create_app(),
        host=LOOPBACK_HOST,
        port=port,
        proxy_headers=False,
        forwarded_allow_ips="",
        access_log=False,
        server_header=False,
        date_header=False,
        log_config=None,
        ws_max_size=MAX_WEBSOCKET_TEXT_BYTES,
        ws_max_queue=WEBSOCKET_QUEUE_CAPACITY,
        ws_ping_interval=None,
        ws_ping_timeout=None,
        lifespan="on",
    )


def serve(*, port: int) -> int:
    """Run one fixed-host Uvicorn server until graceful process shutdown."""

    server: _LoopbackServer | None = None

    def request_server_exit() -> None:
        """Request Uvicorn exit after the shutdown HTTP response is flushed."""

        if server is None:
            raise RuntimeError("loopback server is not initialized")
        server.should_exit = True

    app = create_app(shutdown_callback=request_server_exit)
    server = _LoopbackServer(build_config(port=port, app=app))
    server.run()
    return 0


__all__ = ["build_config", "serve"]
