"""Bounded asynchronous request dispatcher for application methods."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from harness_shell_sidecar.protocol import FrameEnvelope, MessageType


Handler = Callable[[FrameEnvelope, asyncio.Event], Awaitable[dict]]


class DispatchError(RuntimeError):
    def __init__(
        self, error_code: str, message: str, *, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class DispatchResult:
    message_type: MessageType
    payload: dict


@dataclass(slots=True)
class _ActiveRequest:
    cancelled: asyncio.Event
    task: asyncio.Task


class RequestDispatcher:
    def __init__(self, *, capacity: int = 16) -> None:
        if capacity <= 0:
            raise ValueError("dispatcher capacity must be positive")
        self._capacity = capacity
        self._handlers: dict[str, Handler] = {}
        self._active: dict[UUID, _ActiveRequest] = {}
        self._closing = False

    def register(self, method: str, handler: Handler) -> None:
        if not method or method in self._handlers:
            raise ValueError(f"handler is already registered: {method!r}")
        self._handlers[method] = handler

    def handles(self, method: object) -> bool:
        return isinstance(method, str) and method in self._handlers

    async def dispatch(self, frame: FrameEnvelope) -> DispatchResult:
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
        active = self._active.get(target_request_id)
        if active is None:
            return False
        active.cancelled.set()
        return True

    async def close(self) -> None:
        self._closing = True
        active = list(self._active.values())
        for request in active:
            request.cancelled.set()
        current = asyncio.current_task()
        tasks = [request.task for request in active if request.task is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
