"""Safe HTTP Problem Details construction and exception mapping."""

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
    """Carry only an explicitly constructed safe HTTP failure."""

    def __init__(self, problem: ProblemDetails) -> None:
        """Retain the strict response model without the originating exception."""

        super().__init__(f"{problem.error_code}: {problem.message}")
        self.problem = problem  # Already-safe public HTTP representation.


def build_problem(
    *,
    request_id: UUID,
    status: int,
    error_code: str,
    title: str,
    message: str,
    details: dict[str, JsonValue] | None = None,
) -> ProblemDetails:
    """Build a bounded error-code-owned Problem Details response."""

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
    """Return a previously validated request ID or a safe generated error ID."""

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
    """Serialize Problem Details with matching media type and correlation header."""

    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
        headers={"X-Request-ID": str(problem.request_id)},
        media_type="application/problem+json",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Replace every FastAPI/Starlette default failure body with typed Problems."""

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
        # Exception text can originate from secret-bearing application inputs. Log
        # only its class and a stable event, never request bodies or raw messages.
        LOGGER.error(
            "http_request_failed error_code=%s exception_type=%s",
            "SIDECAR_RUNTIME_FAILED",
            type(error).__name__,
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
