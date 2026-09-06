"""独立于传输的请求标识与协作式取消。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID


class RequestCancelledError(RuntimeError):
    """表示 dispatcher 拥有的取消事件在派发前已触发。"""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """携带请求标识及 dispatcher 拥有的取消信号。"""

    #: 入站适配器、应用和响应共享的稳定标识。
    request_id: UUID
    #: dispatcher 拥有的信号，在外部副作用开始前检查。
    cancelled: asyncio.Event

    def require_active(self) -> None:
        """取消先发生时抛出稳定派发取消错误。"""

        if self.cancelled.is_set():
            raise RequestCancelledError(
                "request was cancelled before application execution"
            )
