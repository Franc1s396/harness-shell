"""Pre-bound loopback Uvicorn process entry for the private Python backend."""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import uvicorn

from harness_shell_sidecar.runtime.desktop_control import DesktopControl
from harness_shell_sidecar.runtime.settings import RuntimeSettings

from .app import create_app
from .websocket import MAX_WEBSOCKET_TEXT_BYTES, WEBSOCKET_QUEUE_CAPACITY


LOGGER = logging.getLogger("harness_shell_sidecar.web.server")
LOOPBACK_HOST = "127.0.0.1"


class _LoopbackServer(uvicorn.Server):
    """Publish readiness only after Uvicorn lifespan and startup both succeed."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        actual_port: int,
        ready_callback: Callable[[int], None] | None = None,
    ) -> None:
        """Retain the pre-bound port and optional one-shot ready callback."""

        super().__init__(config)
        self._actual_port = actual_port
        self._ready_callback = ready_callback

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Bind first, then publish the allowlisted host and port."""

        await super().startup(sockets=sockets)
        if self.started:
            LOGGER.info(
                "http_server_listening host=%s port=%s",
                LOOPBACK_HOST,
                self._actual_port,
            )
            if self._ready_callback is not None:
                self._ready_callback(self._actual_port)


def build_config(*, port: int, app) -> uvicorn.Config:
    """Build the only accepted private-loopback Uvicorn configuration."""

    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    return uvicorn.Config(
        app,
        host=LOOPBACK_HOST,
        port=port,
        proxy_headers=False,
        forwarded_allow_ips="",
        access_log=False,
        server_header=False,
        date_header=False,
        log_config=None,
        log_level="warning",
        ws_max_size=MAX_WEBSOCKET_TEXT_BYTES,
        ws_max_queue=WEBSOCKET_QUEUE_CAPACITY,
        ws_ping_interval=None,
        ws_ping_timeout=None,
        lifespan="on",
    )


def _prebind_listener(port: int) -> tuple[socket.socket, int]:
    """Bind and listen before Uvicorn so port ownership never has a race gap."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((LOOPBACK_HOST, port))
        listener.listen(socket.SOMAXCONN)
        actual_port = int(listener.getsockname()[1])
        return listener, actual_port
    except BaseException:
        listener.close()
        raise


def _run_server(
    *,
    port: int,
    data_dir: Path,
    desktop_control: DesktopControl | None = None,
) -> int:
    """Run one autonomous Runtime on an already-owned loopback listener."""

    settings = RuntimeSettings.from_data_dir(data_dir)
    app = create_app(settings=settings)
    listener, actual_port = _prebind_listener(port)
    instance_id = uuid4()
    control_error: list[BaseException] = []

    def publish_ready(bound_port: int) -> None:
        """Publish the only desktop readiness frame after Uvicorn startup."""

        if desktop_control is not None:
            desktop_control.publish_ready(
                instance_id=instance_id,
                port=bound_port,
            )

    server = _LoopbackServer(
        build_config(port=port, app=app),
        actual_port=actual_port,
        ready_callback=publish_ready if desktop_control is not None else None,
    )
    watcher: threading.Thread | None = None
    if desktop_control is not None:

        def watch_control_pipe() -> None:
            """Convert the strict parent control signal into Uvicorn draining."""

            try:
                desktop_control.wait_for_shutdown()
            except BaseException as error:
                control_error.append(error)
            finally:
                server.should_exit = True

        watcher = threading.Thread(
            target=watch_control_pipe,
            name="desktop-control-watcher",
            daemon=True,
        )
        watcher.start()

    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
    if watcher is not None:
        watcher.join(timeout=1)
    if control_error:
        raise control_error[0]
    return 0


def serve(*, port: int, data_dir: Path) -> int:
    """Run development mode on one explicit nonzero loopback port."""

    if not 1 <= port <= 65_535:
        raise ValueError("serve port must be between 1 and 65535")
    return _run_server(port=port, data_dir=data_dir)


def desktop(
    *,
    port: int,
    data_dir: Path,
    control_read_handle: int,
    ready_write_handle: int,
) -> int:
    """Run packaged desktop mode with dynamic port and inherited controls."""

    if port != 0:
        raise ValueError("desktop port must be 0")
    control = DesktopControl(control_read_handle, ready_write_handle)
    try:
        return _run_server(
            port=port,
            data_dir=data_dir,
            desktop_control=control,
        )
    finally:
        control.close()


__all__ = ["build_config", "desktop", "serve"]
