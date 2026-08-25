"""Fail-closed runtime request router."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

from harness_shell_sidecar.protocol import (
    FrameEnvelope,
    MessageType,
    ProtocolViolation,
    Sensitivity,
)

from .messages import (
    InitializeRequestPayload,
    RuntimeCapabilities,
    RuntimePhase,
)


Initializer = Callable[[InitializeRequestPayload], None]


class Router:
    def __init__(self, initializer: Initializer | None = None) -> None:
        self.phase = RuntimePhase.STARTING
        self._initializer = initializer or (lambda payload: None)
        self._next_inbound_sequence = 1
        self._next_outbound_sequence = 1

    @property
    def should_stop(self) -> bool:
        return self.phase in (RuntimePhase.STOPPING, RuntimePhase.FAILED)

    def ready_event(self) -> FrameEnvelope:
        if self.phase is not RuntimePhase.STARTING:
            raise ProtocolViolation("sidecar.ready may only be emitted once")
        self.phase = RuntimePhase.HANDSHAKING
        capabilities = RuntimeCapabilities().model_dump(mode="json")
        return self._outbound(
            request_id=uuid4(),
            message_type=MessageType.EVENT,
            payload={"event": "sidecar.ready", "capabilities": capabilities},
        )

    def handle(self, frame: FrameEnvelope) -> FrameEnvelope:
        self._validate_inbound_sequence(frame.sequence)

        if frame.message_type is MessageType.HEARTBEAT:
            return self._handle_heartbeat(frame)
        if frame.message_type is MessageType.CANCEL:
            return self._handle_cancel(frame)
        if frame.message_type is not MessageType.REQUEST:
            return self._error(
                frame,
                "UNEXPECTED_MESSAGE_TYPE",
                "message type is not accepted in this runtime phase",
            )

        method = frame.payload.get("method")
        if method == "initialize":
            return self._handle_initialize(frame)
        if method == "shutdown":
            return self._handle_shutdown(frame)
        return self._error(frame, "UNKNOWN_METHOD", "request method is unknown")

    def terminal_error(self, error_code: str, message: str) -> FrameEnvelope:
        self.phase = RuntimePhase.FAILED
        return self._outbound(
            request_id=uuid4(),
            message_type=MessageType.ERROR,
            payload={"error_code": error_code, "message": message},
        )

    def mark_stopped(self) -> None:
        if self.phase is not RuntimePhase.FAILED:
            self.phase = RuntimePhase.STOPPED

    def _handle_initialize(self, frame: FrameEnvelope) -> FrameEnvelope:
        if self.phase is not RuntimePhase.HANDSHAKING:
            return self._error(
                frame,
                "INVALID_RUNTIME_PHASE",
                "initialize is only accepted during handshake",
            )
        if frame.sensitivity is not Sensitivity.SECRET:
            return self._error(
                frame,
                "SENSITIVE_FRAME_REQUIRED",
                "initialize must use a secret frame",
            )

        try:
            payload = InitializeRequestPayload.model_validate_json(
                json.dumps(frame.payload, separators=(",", ":"), ensure_ascii=False)
            )
        except (ValidationError, ValueError, TypeError):
            return self._error(
                frame,
                "INVALID_INITIALIZE_PAYLOAD",
                "initialize payload is invalid",
            )

        try:
            self._initializer(payload)
        except Exception:
            response = self._error(
                frame,
                "RUNTIME_INITIALIZATION_FAILED",
                "runtime initialization failed",
            )
            self.phase = RuntimePhase.FAILED
            return response

        self.phase = RuntimePhase.READY
        return self._outbound_for(
            frame,
            MessageType.RESPONSE,
            {"result": "initialized", "state": RuntimePhase.READY.value},
        )

    def _handle_heartbeat(self, frame: FrameEnvelope) -> FrameEnvelope:
        if self.phase is not RuntimePhase.READY or frame.payload != {"kind": "ping"}:
            return self._error(
                frame,
                "INVALID_HEARTBEAT",
                "heartbeat is invalid for the current runtime phase",
            )
        return self._outbound_for(
            frame,
            MessageType.HEARTBEAT,
            {"kind": "pong"},
        )

    def _handle_cancel(self, frame: FrameEnvelope) -> FrameEnvelope:
        target = frame.payload.get("target_request_id")
        reason = frame.payload.get("reason")
        try:
            UUID(str(target))
        except (ValueError, TypeError, AttributeError):
            return self._error(
                frame,
                "INVALID_CANCEL_PAYLOAD",
                "cancel payload is invalid",
            )
        if reason != "user_requested":
            return self._error(
                frame,
                "INVALID_CANCEL_PAYLOAD",
                "cancel payload is invalid",
            )
        return self._error(
            frame,
            "CANCEL_TARGET_NOT_FOUND",
            "target request is not active",
        )

    def _handle_shutdown(self, frame: FrameEnvelope) -> FrameEnvelope:
        if self.phase is not RuntimePhase.READY or frame.payload != {
            "method": "shutdown"
        }:
            return self._error(
                frame,
                "INVALID_RUNTIME_PHASE",
                "shutdown is only accepted while ready",
            )
        self.phase = RuntimePhase.STOPPING
        return self._outbound_for(
            frame,
            MessageType.RESPONSE,
            {"result": "stopping"},
        )

    def _validate_inbound_sequence(self, actual: int) -> None:
        expected = self._next_inbound_sequence
        if actual != expected:
            raise ProtocolViolation(
                f"inbound sequence mismatch: expected {expected}, got {actual}"
            )
        self._next_inbound_sequence += 1

    def _error(
        self, frame: FrameEnvelope, error_code: str, message: str
    ) -> FrameEnvelope:
        return self._outbound_for(
            frame,
            MessageType.ERROR,
            {"error_code": error_code, "message": message},
        )

    def _outbound_for(
        self,
        inbound: FrameEnvelope,
        message_type: MessageType,
        payload: dict,
    ) -> FrameEnvelope:
        return self._outbound(
            request_id=inbound.request_id,
            message_type=message_type,
            payload=payload,
            task_id=inbound.task_id,
            workflow_run_id=inbound.workflow_run_id,
        )

    def _outbound(
        self,
        *,
        request_id: UUID,
        message_type: MessageType,
        payload: dict,
        task_id: UUID | None = None,
        workflow_run_id: UUID | None = None,
    ) -> FrameEnvelope:
        frame = FrameEnvelope(
            protocol_version=1,
            message_type=message_type,
            request_id=request_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            sequence=self._next_outbound_sequence,
            timestamp=datetime.now(timezone.utc),
            sensitivity=Sensitivity.NORMAL,
            payload=payload,
        )
        self._next_outbound_sequence += 1
        return frame
