"""具有有界路由与请求关联字段的 HTTP 访问日志。"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from uuid import UUID, uuid4

from harness_shell_sidecar.telemetry import bind_request_id, reset_request_id


LOGGER = logging.getLogger("harness_shell_sidecar.web.access")
AsgiMessage = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[AsgiMessage]]
Send = Callable[[AsgiMessage], Awaitable[None]]
AsgiApp = Callable[[MutableMapping[str, Any], Receive, Send], Awaitable[None]]


class HttpAccessLogMiddleware:
    """每个 HTTP 请求发出一条完成记录，不记录载荷。"""

    def __init__(self, app: AsgiApp) -> None:
        """包装下游 ASGI 应用，不改变其生命周期。"""

        self._app = app  # 借用的下游 ASGI 应用。

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        """捕获响应状态，并在完成后记录规范路由。"""

        # 1. 非 HTTP 流量直接透传；HTTP 请求才建立关联上下文。
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # 2. 绑定请求 ID 并开始计时，发送包装器捕获真实响应状态。
        request_id = _request_id(scope)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        token = bind_request_id(request_id)
        started = time.monotonic_ns()
        status_code = 500

        async def capture_status(message: AsgiMessage) -> None:
            """转发 ASGI 消息时记住精确 HTTP 状态。"""

            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, capture_status)
        # 3. 请求结束后记录允许的路由与状态信息，并恢复外层关联上下文。
        finally:
            duration_ms = (time.monotonic_ns() - started) // 1_000_000
            route = scope.get("route")
            route_path = getattr(route, "path", "<unmatched>")
            method = str(scope.get("method", "<unknown>"))
            if route_path != "/v1/runtime/state":
                message = (
                    "http_request_completed method=%s route=%s "
                    "status=%s duration_ms=%s"
                )
                if status_code >= 500:
                    LOGGER.error(
                        message, method, route_path, status_code, duration_ms
                    )
                elif status_code >= 400:
                    LOGGER.warning(
                        message, method, route_path, status_code, duration_ms
                    )
                else:
                    LOGGER.info(
                        message, method, route_path, status_code, duration_ms
                    )
            reset_request_id(token)


def _request_id(scope: MutableMapping[str, Any]) -> UUID:
    """返回标准请求头标识，或为被拒绝请求生成标识。"""

    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() != b"x-request-id":
            continue
        try:
            return UUID(raw_value.decode("ascii", errors="strict"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            break
    return uuid4()
