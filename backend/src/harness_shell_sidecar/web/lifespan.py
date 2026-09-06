"""唯一已初始化运行时资源图的 ASGI lifespan 管理者。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from harness_shell_sidecar.runtime.models import RuntimePhase
from harness_shell_sidecar.runtime.resources import EventSink, RuntimeResources
from harness_shell_sidecar.runtime.settings import RuntimeSettings

from .websocket import RuntimeWebSocketGateway


ResourceFactory = Callable[[RuntimeSettings, EventSink], RuntimeResources]


class RuntimeOwnerError(RuntimeError):
    """描述运行时生命周期管理者上的稳定非法操作错误。"""

    def __init__(self, error_code: str, message: str) -> None:
        """仅保留稳定错误码和安全公开生命周期消息。"""

        super().__init__(message)
        self.error_code = error_code  # HTTP 映射使用的稳定标识。
        self.public_message = message  # 不含资源详情的安全有界文本。
        self.safe_message = message  # 内部诊断别名。


class RuntimeOwner:
    """初始化、暴露并收敛且仅收敛一个 RuntimeResources 实例。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        resource_factory: ResourceFactory,
    ) -> None:
        """创建必须在 ASGI 接收请求前初始化的管理者。"""

        self._settings = settings  # 根据 CLI 数据目录派生的固定路径。
        self._resource_factory = resource_factory  # 原子资源图构造器。
        self._resources: RuntimeResources | None = None  # 唯一资源图引用。
        self._state = RuntimePhase.INITIALIZING  # 共享公开阶段。
        self._start_attempted = False  # lifespan 启动只能执行一次。
        self.websocket_gateway = RuntimeWebSocketGateway()

    async def start(self, event_sink: EventSink) -> RuntimeResources:
        """在 ASGI 接收请求前初始化全部资源。"""

        if self._start_attempted:
            raise RuntimeOwnerError(
                "RUNTIME_ALREADY_INITIALIZED",
                "Runtime initialization has already been attempted",
            )
        self._start_attempted = True
        try:
            resources = self._resource_factory(self._settings, event_sink)
        except BaseException:
            self._state = RuntimePhase.FAILED
            raise
        self._resources = resources
        self._state = RuntimePhase.READY
        return resources

    async def event_sink(self, event: dict[str, object]) -> None:
        """对领域事件施加有界背压，直至 WebSocket 交付。"""

        await self.websocket_gateway.publish_domain_event(event)

    def state(self) -> RuntimePhase:
        """返回当前安全生命周期阶段。"""

        return self._state

    def require_resources(self) -> RuntimeResources:
        """返回就绪资源图，否则明确拒绝应用工作。"""

        if self._state is not RuntimePhase.READY or self._resources is None:
            raise RuntimeOwnerError("RUNTIME_NOT_READY", "Runtime is not ready")
        return self._resources

    async def shutdown(self) -> RuntimePhase:
        """仅收敛一次资源图，并保留终态失败状态。"""

        resources = self._resources
        if resources is None:
            return self._state
        if self._state in (RuntimePhase.STOPPED, RuntimePhase.FAILED):
            return self._state
        self._state = RuntimePhase.DRAINING
        try:
            await resources.shutdown()
        except BaseException:
            self._state = RuntimePhase.FAILED
            raise
        finally:
            self._resources = None
        self._state = RuntimePhase.STOPPED
        return self._state


def default_resource_factory(
    settings: RuntimeSettings,
    event_sink: EventSink,
) -> RuntimeResources:
    """构建明文资源图，不注入 Runtime 密钥。"""

    return RuntimeResources.initialize_from_settings(settings, event_sink)


@asynccontextmanager
async def runtime_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """在整个 ASGI 应用生命周期中拥有唯一 RuntimeOwner。"""

    factory = app.state.resource_factory
    owner = RuntimeOwner(app.state.settings, factory)
    app.state.runtime_owner = owner
    await owner.start(owner.event_sink)
    try:
        yield
    finally:
        await owner.shutdown()
