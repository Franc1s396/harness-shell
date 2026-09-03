"""Safe SSH runtime errors and public connection status models."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from harness_shell_sidecar.connections import HostKeyCandidate


RemoteState = Literal[
    "not_contacted", "pre_auth", "authenticated", "channel_dispatched", "unknown"
]


class ConnectionStatus(BaseModel):
    """对桌面端公开的 SSH 连接状态快照。"""

    #: 禁止额外字段和类型隐式转换。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 状态所属的连接配置标识符。
    connection_id: UUID
    #: 连接生命周期中的当前有限状态。
    state: Literal[
        "DISCONNECTED",
        "CONNECTING",
        "HOST_KEY_REQUIRED",
        "READY",
        "CLOSING",
        "FAILED",
    ]
    #: READY 会话标识符；尚未就绪或已关闭时为空。
    session_id: UUID | None
    #: 最近失败的稳定错误码；非失败状态时为空。
    error_code: str | None
    #: 调用方修正输入后是否允许显式重试。
    recoverable: bool
    #: 关联本次 SSH 操作、审计事件与 Span 的标识符。
    correlation_id: UUID
    #: 首次观察或不匹配时供用户确认的新 Host Key。
    host_key_candidate: HostKeyCandidate | None
    #: Host Key 不匹配时当前受信任记录的指纹。
    trusted_fingerprint_sha256: str | None = None


class SshRuntimeError(RuntimeError):
    """SSH 运行时可安全跨 IPC 公开的结构化失败。"""

    def __init__(
        self,
        error_code: str,
        *,
        node: str,
        recoverable: bool,
        remote_state: RemoteState,
        correlation_id: UUID | None = None,
        candidate: HostKeyCandidate | None = None,
        trusted_fingerprint_sha256: str | None = None,
    ) -> None:
        """保存失败节点、远端副作用状态和可恢复性等诊断上下文。"""

        super().__init__("SSH operation failed")
        self.error_code = error_code  # 面向调用方的稳定失败类别。
        self.node = node  # 失败发生的连接流程节点。
        self.recoverable = recoverable  # 是否允许修正输入后显式重试。
        self.remote_state = remote_state  # 失败时可确认的远端副作用阶段。
        self.correlation_id = correlation_id or uuid4()  # SSH 状态与错误的关联标识符。
        self.candidate = candidate  # 可选的新观察 Host Key。
        # Host Key 不匹配时供 UI 对比的受信任指纹。
        self.trusted_fingerprint_sha256 = trusted_fingerprint_sha256

    def public_payload(self) -> dict:
        """构造不包含凭据和底层异常文本的公开错误负载。"""

        payload: dict = {
            "error_code": self.error_code,
            "node": self.node,
            "recoverable": self.recoverable,
            "correlation_id": str(self.correlation_id),
            "remote_state": self.remote_state,
        }
        if self.candidate is not None:
            payload["host_key_candidate"] = self.candidate.model_dump(mode="json")
        if self.trusted_fingerprint_sha256 is not None:
            payload["trusted_fingerprint_sha256"] = self.trusted_fingerprint_sha256
        return payload
