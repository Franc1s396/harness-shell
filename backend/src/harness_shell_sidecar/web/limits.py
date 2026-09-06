"""编码后的 HTTP 请求与响应大小硬边界。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware

from harness_shell_sidecar.runtime.models import MAX_JSON_BODY_BYTES

from .errors import build_problem, problem_response, request_correlation_id


MAX_JSON_BYTES = MAX_JSON_BODY_BYTES


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """在 FastAPI 模型校验前拒绝超限编码请求体。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """计算完整已接收正文，绝不截断或摘要。"""

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_JSON_BYTES:
                    return self._too_large(request)
            except ValueError:
                pass
        body = await request.body()
        if len(body) > MAX_JSON_BYTES:
            return self._too_large(request)
        return await call_next(request)

    @staticmethod
    def _too_large(request: Request) -> Response:
        """返回有界的 typed 413 响应。"""

        return problem_response(
            build_problem(
                request_id=request_correlation_id(request),
                status=413,
                error_code="REQUEST_TOO_LARGE",
                title="Request too large",
                message="Encoded request body exceeds the endpoint limit",
            )
        )


class ResponseLimitRoute(APIRoute):
    """ASGI 发送任何字节前拒绝超限编码 JSON 路由响应。"""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        """包装生成的 FastAPI 路由 handler，执行最终字节检查。"""

        original = super().get_route_handler()

        async def limited(request: Request) -> Response:
            response = await original(request)
            body = getattr(response, "body", None)
            if isinstance(body, bytes) and len(body) > MAX_JSON_BYTES:
                return problem_response(
                    build_problem(
                        request_id=request_correlation_id(request),
                        status=413,
                        error_code="RESPONSE_TOO_LARGE",
                        title="Response too large",
                        message="Encoded response body exceeds the endpoint limit",
                    )
                )
            return response

        return limited
