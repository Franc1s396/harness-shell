"""Python 内部后端预绑定 loopback 的 Uvicorn 进程入口。"""

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
    """Uvicorn lifespan 和启动均成功后才发布就绪。"""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        actual_port: int,
        ready_callback: Callable[[int], None] | None = None,
    ) -> None:
        """保留预绑定端口及可选的一次性就绪回调。"""

        super().__init__(config)
        self._actual_port = actual_port
        self._ready_callback = ready_callback

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """先绑定，再发布允许的 host 和端口。"""

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
    """构建唯一允许的内部 loopback Uvicorn 配置。"""

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
    """在 Uvicorn 启动前绑定并监听，消除端口所有权竞态空窗。"""

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
    """在已拥有的 loopback 监听器上运行自主初始化的 Runtime。"""

    # 1. 构建固定配置并预绑定监听器，先取得端口所有权。
    settings = RuntimeSettings.from_data_dir(data_dir)
    app = create_app(settings=settings)
    listener, actual_port = _prebind_listener(port)
    instance_id = uuid4()
    control_error: list[BaseException] = []

    def publish_ready(bound_port: int) -> None:
        """Uvicorn 启动后发布唯一桌面就绪帧。"""

        if desktop_control is not None:
            desktop_control.publish_ready(
                instance_id=instance_id,
                port=bound_port,
            )

    # 2. 就绪发布交给 Uvicorn 启动回调，避免资源未就绪即通知 Launcher。
    server = _LoopbackServer(
        build_config(port=port, app=app),
        actual_port=actual_port,
        ready_callback=publish_ready if desktop_control is not None else None,
    )
    # 3. 桌面模式独占控制管道线程，把父进程信号转换为关闭请求。
    watcher: threading.Thread | None = None
    if desktop_control is not None:

        def watch_control_pipe() -> None:
            """将严格父进程控制信号转换为 Uvicorn 排空关闭。"""

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
        # 4. 使用已绑定监听器运行；退出时关闭监听器并检查控制线程错误。
        server.run(sockets=[listener])
    finally:
        listener.close()
    if watcher is not None:
        watcher.join(timeout=1)
    if control_error:
        raise control_error[0]
    return 0


def serve(*, port: int, data_dir: Path) -> int:
    """在显式非零 loopback 端口上运行开发模式。"""

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
    """使用动态端口和继承控制句柄运行打包版桌面模式。"""

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
