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
    RuntimeInitializationFailure,
    RuntimePhase,
)


Initializer = Callable[[InitializeRequestPayload], None]


class Router:
    """执行协议序号校验和 Sidecar 生命周期状态转换的 fail-closed 路由器。"""

    def __init__(self, initializer: Initializer | None = None) -> None:
        """创建处于 STARTING 阶段且双向序号均从 1 开始的路由器。"""

        self.phase = RuntimePhase.STARTING  # 当前公开运行阶段。
        self._initializer = initializer or (lambda payload: None)  # 资源初始化入口。
        self._next_inbound_sequence = 1  # 下一帧必须携带的入站序号。
        self._next_outbound_sequence = 1  # 下一帧将分配的出站序号。

    @property
    def should_stop(self) -> bool:
        """返回运行循环是否应因停止中或失败状态退出。"""

        return self.phase in (RuntimePhase.STOPPING, RuntimePhase.FAILED)

    def ready_event(self) -> FrameEnvelope:
        """仅发布一次 Sidecar 能力事件，并进入握手阶段。"""

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
        """校验入站序号后处理一个生命周期或控制帧。"""

        self.validate_inbound(frame)
        return self.handle_validated(frame)

    def validate_inbound(self, frame: FrameEnvelope) -> None:
        """校验并消费一个严格连续的入站帧序号。"""

        self._validate_inbound_sequence(frame.sequence)

    def handle_validated(self, frame: FrameEnvelope) -> FrameEnvelope:
        """处理已完成序号校验的帧，不再次推进入站序号。"""

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

    def application_response(
        self,
        frame: FrameEnvelope,
        message_type: MessageType,
        payload: dict,
    ) -> FrameEnvelope:
        """把应用处理结果关联到原始请求并分配出站序号。"""

        if message_type not in (MessageType.RESPONSE, MessageType.ERROR):
            raise ValueError("application response must be response or error")
        return self._outbound_for(frame, message_type, payload)

    def application_event(self, payload: dict) -> FrameEnvelope:
        """在 READY 阶段创建无原始请求关联的应用事件帧。"""

        if self.phase is not RuntimePhase.READY:
            raise ValueError("application events require a ready runtime")
        return self._outbound(
            request_id=uuid4(),
            message_type=MessageType.EVENT,
            payload=payload,
        )

    def cancel_target(self, frame: FrameEnvelope) -> UUID:
        """校验取消原因并解析目标 request_id。"""

        target = frame.payload.get("target_request_id")
        reason = frame.payload.get("reason")
        if reason != "user_requested":
            raise ValueError("cancel reason is invalid")
        return UUID(str(target))

    def terminal_error(self, error_code: str, message: str) -> FrameEnvelope:
        """进入 FAILED 阶段并创建终止性错误帧。"""

        self.phase = RuntimePhase.FAILED
        return self._outbound(
            request_id=uuid4(),
            message_type=MessageType.ERROR,
            payload={"error_code": error_code, "message": message},
        )

    def mark_stopped(self) -> None:
        """在非失败退出路径上将路由器标记为 STOPPED。"""

        if self.phase is not RuntimePhase.FAILED:
            self.phase = RuntimePhase.STOPPED

    def _handle_initialize(self, frame: FrameEnvelope) -> FrameEnvelope:
        """校验敏感初始化帧、建立资源并转换到 READY。"""

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
        except RuntimeInitializationFailure as exc:
            response = self._error(frame, exc.error_code, exc.public_message)
            self.phase = RuntimePhase.FAILED
            return response
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
        """仅在 READY 阶段接受精确 ping 负载并返回 pong。"""

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
        """处理路由器自身无法命中的取消目标。"""

        try:
            self.cancel_target(frame)
        except (ValueError, TypeError, AttributeError):
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
        """仅在 READY 阶段接受精确 shutdown 请求并进入 STOPPING。"""

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
        """拒绝乱序、跳号或重放，并推进期望序号。"""

        expected = self._next_inbound_sequence
        if actual != expected:
            raise ProtocolViolation(
                f"inbound sequence mismatch: expected {expected}, got {actual}"
            )
        self._next_inbound_sequence += 1

    def _error(
        self, frame: FrameEnvelope, error_code: str, message: str
    ) -> FrameEnvelope:
        """创建与入站请求关联的标准错误响应。"""

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
        """继承入站帧的请求与工作流关联信息来创建响应。"""

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
        """创建带 UTC 时间和下一出站序号的普通敏感级别帧。"""

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
