"""HTTP access logging with bounded route and request correlation fields."""

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
    """Emit one completion record for each HTTP request without logging payloads."""

    def __init__(self, app: AsgiApp) -> None:
        """Wrap the downstream ASGI application without changing its lifecycle."""

        self._app = app  # Borrowed downstream ASGI application.

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        """Capture response status and log the normalized route after completion."""

        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = _request_id(scope)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        token = bind_request_id(request_id)
        started = time.monotonic_ns()
        status_code = 500

        async def capture_status(message: AsgiMessage) -> None:
            """Remember the exact HTTP status while forwarding the ASGI message."""

            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, capture_status)
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
    """Return the canonical request header or generate one for rejected requests."""

    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() != b"x-request-id":
            continue
        try:
            return UUID(raw_value.decode("ascii", errors="strict"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            break
    return uuid4()
