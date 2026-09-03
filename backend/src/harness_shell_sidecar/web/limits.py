"""Hard encoded HTTP request and response size boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware

from harness_shell_sidecar.runtime.models import MAX_JSON_BODY_BYTES

from .errors import build_problem, problem_response, request_correlation_id


MAX_JSON_BYTES = MAX_JSON_BODY_BYTES


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject an encoded request body before FastAPI model validation."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Count the complete received body and never truncate or summarize it."""

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
        """Return the bounded typed 413 response."""

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
    """Reject an encoded JSON route response before ASGI sends any bytes."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        """Wrap the generated FastAPI route handler with a final byte check."""

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
