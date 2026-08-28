"""Bounded asynchronous request dispatcher for application methods."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from harness_shell_sidecar.protocol import FrameEnvelope, MessageType


Handler = Callable[[FrameEnvelope, asyncio.Event], Awaitable[dict]]


class DispatchError(RuntimeError):
    """应用请求分发失败时返回给协议层的结构化异常。"""

    def __init__(
        self, error_code: str, message: str, *, details: dict | None = None
    ) -> None:
        """保存稳定错误码、公开消息和可选结构化详情。"""

        super().__init__(message)
        self.error_code = error_code  # 映射到协议错误负载的稳定标识符。
        self.details = details or {}  # 可选的非敏感机器可读错误上下文。


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """处理器完成后交回服务循环的协议响应描述。"""

    #: 要发送的响应帧类型，通常为 response 或 error。
    message_type: MessageType
    #: 写入响应信封的业务负载。
    payload: dict


@dataclass(slots=True)
class _ActiveRequest:
    """分发器为一个进行中请求保存的取消与任务状态。"""

    #: 通知业务处理器协作式停止工作的事件。
    cancelled: asyncio.Event
    #: 正在执行该请求的 asyncio 任务。
    task: asyncio.Task


class RequestDispatcher:
    """以固定并发容量注册、执行、取消并收敛应用请求。"""

    def __init__(self, *, capacity: int = 16) -> None:
        """创建具有正数并发上限且尚未注册处理器的分发器。"""

        if capacity <= 0:
            raise ValueError("dispatcher capacity must be positive")
        self._capacity = capacity  # 允许同时处于活动状态的请求数上限。
        self._handlers: dict[str, Handler] = {}  # method 到异步处理器的固定映射。
        self._active: dict[UUID, _ActiveRequest] = {}  # 按 request_id 跟踪进行中请求。
        self._closing = False  # 关闭开始后拒绝接受新请求。

    def register(self, method: str, handler: Handler) -> None:
        """为唯一且非空的 method 名称注册一个异步处理器。"""

        if not method or method in self._handlers:
            raise ValueError(f"handler is already registered: {method!r}")
        self._handlers[method] = handler

    def handles(self, method: object) -> bool:
        """判断输入是否为当前分发器已注册的 method。"""

        return isinstance(method, str) and method in self._handlers

    async def dispatch(self, frame: FrameEnvelope) -> DispatchResult:
        """执行一个已校验请求，并保证完成后清除活动状态。"""

        if self._closing:
            raise DispatchError("RUNTIME_STOPPING", "runtime is stopping")
        method = frame.payload.get("method")
        if not isinstance(method, str) or method not in self._handlers:
            raise DispatchError("UNKNOWN_METHOD", "request method is unknown")
        if frame.request_id in self._active:
            raise DispatchError(
                "DUPLICATE_REQUEST_ID", "request ID is already active"
            )
        if len(self._active) >= self._capacity:
            raise DispatchError(
                "REQUEST_CAPACITY_EXCEEDED", "active request capacity is exhausted"
            )

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("dispatcher requires an asyncio task")
        cancelled = asyncio.Event()
        self._active[frame.request_id] = _ActiveRequest(cancelled, task)
        try:
            payload = await self._handlers[method](frame, cancelled)
            if not isinstance(payload, dict):
                raise DispatchError(
                    "INVALID_HANDLER_RESULT", "handler result must be an object"
                )
            return DispatchResult(MessageType.RESPONSE, payload)
        finally:
            self._active.pop(frame.request_id, None)

    async def cancel(self, target_request_id: UUID) -> bool:
        """设置目标请求的协作式取消事件，并返回目标是否存在。"""

        active = self._active.get(target_request_id)
        if active is None:
            return False
        active.cancelled.set()
        return True

    async def close(self) -> None:
        """拒绝新请求、取消全部活动请求并等待其任务收敛。"""

        self._closing = True
        active = list(self._active.values())
        for request in active:
            request.cancelled.set()
        current = asyncio.current_task()
        tasks = [request.task for request in active if request.task is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
