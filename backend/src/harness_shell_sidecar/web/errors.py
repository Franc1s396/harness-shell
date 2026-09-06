"""安全 HTTP Problem Details 构造与异常映射。"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

from .models import JsonValue, ProblemDetails


LOGGER = logging.getLogger("harness_shell_sidecar.web")


class HttpProblem(RuntimeError):
    """只携带显式构建的安全 HTTP 失败。"""

    def __init__(self, problem: ProblemDetails) -> None:
        """保留严格响应模型，不保留原始异常。"""

        super().__init__(f"{problem.error_code}: {problem.message}")
        self.problem = problem  # 已确保安全的公开 HTTP 表示。


def build_problem(
    *,
    request_id: UUID,
    status: int,
    error_code: str,
    title: str,
    message: str,
    details: dict[str, JsonValue] | None = None,
) -> ProblemDetails:
    """构建由错误码决定的有界 Problem Details 响应。"""

    slug = error_code.lower().replace("_", "-")
    return ProblemDetails(
        type=f"urn:harness-shell:error:{slug}",
        title=title,
        status=status,
        error_code=error_code,
        message=message,
        request_id=request_id,
        details=details or {},
    )


def request_correlation_id(request: Request) -> UUID:
    """返回此前已校验请求 ID，或安全生成的错误 ID。"""

    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, UUID):
        return existing
    raw = request.headers.get("X-Request-ID")
    if raw is not None:
        try:
            return UUID(raw)
        except (ValueError, AttributeError):
            pass
    generated = uuid4()
    request.state.request_id = generated
    return generated


def problem_response(problem: ProblemDetails) -> JSONResponse:
    """使用匹配媒体类型和关联头序列化 Problem Details。"""

    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
        headers={"X-Request-ID": str(problem.request_id)},
        media_type="application/problem+json",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """将全部 FastAPI/Starlette 默认失败正文替换为 typed Problem。"""

    @app.exception_handler(HttpProblem)
    async def handle_http_problem(
        _request: Request, error: HttpProblem
    ) -> JSONResponse:
        return problem_response(error.problem)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        malformed = any(item.get("type") == "json_invalid" for item in error.errors())
        status = 400 if malformed else 422
        code = "MALFORMED_JSON" if malformed else "REQUEST_VALIDATION_FAILED"
        title = "Malformed JSON" if malformed else "Request validation failed"
        fields = sorted(
            {
                ".".join(str(value) for value in item.get("loc", ()))
                for item in error.errors()
                if item.get("loc")
            }
        )
        return problem_response(
            build_problem(
                request_id=request_correlation_id(request),
                status=status,
                error_code=code,
                title=title,
                message="Request payload is invalid",
                details={"fields": fields},
            )
        )

    @app.exception_handler(StarletteHttpException)
    async def handle_http_status(
        request: Request, error: StarletteHttpException
    ) -> JSONResponse:
        status = error.status_code
        code = "ROUTE_NOT_FOUND" if status == 404 else "HTTP_REQUEST_REJECTED"
        return problem_response(
            build_problem(
                request_id=request_correlation_id(request),
                status=status,
                error_code=code,
                title="Route not found" if status == 404 else "Request rejected",
                message="The requested HTTP operation is not available",
            )
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> JSONResponse:
        # 回调显式传入原始异常以保留 traceback；公开响应仍使用固定安全文本。
        LOGGER.exception(
            "http_request_failed error_code=%s exception_type=%s",
            "SIDECAR_RUNTIME_FAILED",
            type(error).__name__,
            exc_info=error,
            extra={
                "harness_event": "http_request_failed",
                "harness_fields": {
                    "error_code": "SIDECAR_RUNTIME_FAILED",
                    "exception_type": type(error).__name__,
                },
            },
        )
        return problem_response(
            build_problem(
                request_id=request_correlation_id(request),
                status=500,
                error_code="SIDECAR_RUNTIME_FAILED",
                title="Runtime request failed",
                message="The runtime request failed",
            )
        )
