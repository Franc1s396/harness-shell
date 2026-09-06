"""严格共享 HTTP 请求与响应模型。"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
)

from harness_shell_sidecar.agent.contracts import (
    ModelApiConfig,
)
from harness_shell_sidecar.connections.models import (
    ConnectionProfile,
    HostKeyRecord,
)
from harness_shell_sidecar.credentials import (
    CredentialPublicKey,
)
from harness_shell_sidecar.manual_sftp.models import MutationProgressProjection
from harness_shell_sidecar.runtime.models import RuntimePhase
from harness_shell_sidecar.ssh.errors import ConnectionStatus
from harness_shell_sidecar.terminal.models import PtySession


class StrictHttpModel(BaseModel):
    """禁止未声明 HTTP 字段和隐式 Python 强制转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class ProblemDetails(StrictHttpModel):
    """暴露有界机器可读失败，不包含原始内部信息。"""

    #: 错误类别的稳定 URN。
    type: str
    #: 简短安全的人类可读类别标签。
    title: str
    #: 重复携带 HTTP 状态，用于严格跨层校验。
    status: int
    #: 稳定的机器可读业务或传输错误码。
    error_code: str
    #: 安全有界消息，不能用作客户端决策依据。
    message: str
    #: 在响应头中回显的关联标识。
    request_id: UUID
    #: 错误码专属允许列表内的结构化上下文。
    details: dict[str, JsonValue]


class HealthLiveResponse(StrictHttpModel):
    """报告 Python HTTP 事件循环能够响应。"""

    request_id: UUID
    live: bool


class HealthReadyResponse(StrictHttpModel):
    """报告完整运行时资源图已就绪。"""

    request_id: UUID
    ready: bool
    state: RuntimePhase


class RuntimeStateResponse(StrictHttpModel):
    """返回安全的共享运行时生命周期状态。"""

    request_id: UUID
    state: RuntimePhase


class CredentialPublicKeyResponse(CredentialPublicKey):
    """返回当前临时公钥及 HTTP 关联信息。"""

    request_id: UUID


class DiagnosticsAvailabilityResponse(StrictHttpModel):
    """仅报告固定 Python 日志目录是否可用。"""

    request_id: UUID = Field(description="HTTP request correlation identity.")
    available: bool = Field(
        description="Whether the fixed Runtime log directory currently exists."
    )


class ConnectionListResponse(StrictHttpModel):
    """按仓库顺序返回全部持久化连接配置。"""

    request_id: UUID
    connections: list[ConnectionProfile]


class ConnectionResponse(StrictHttpModel):
    """返回持久化连接配置。"""

    request_id: UUID
    connection: ConnectionProfile


class DeleteResponse(StrictHttpModel):
    """返回针对标识的已确定删除结果。"""

    request_id: UUID
    deleted: bool


class HostKeyResponse(StrictHttpModel):
    """返回持久化 Host Key 信任记录。"""

    request_id: UUID
    host_key: HostKeyRecord


class SshStatusResponse(StrictHttpModel):
    """返回安全 SSH 生命周期状态。"""

    request_id: UUID
    status: ConnectionStatus


class PtySessionResponse(StrictHttpModel):
    """返回交互式 PTY 会话快照。"""

    request_id: UUID
    pty_session: PtySession


class AgentApiConfigListResponse(StrictHttpModel):
    """返回全部非秘密 Provider 配置。"""

    request_id: UUID
    configs: list[ModelApiConfig]


class AgentApiConfigResponse(StrictHttpModel):
    """返回一个非秘密 Provider 配置。"""

    request_id: UUID
    config: ModelApiConfig


class RuntimeMessageBase(StrictHttpModel):
    """携带所有严格 Runtime WebSocket 消息共用的字段。"""

    #: 冻结的 WebSocket schema 版本。
    schema_version: Literal[1] = 1
    #: 用于因果关联和 PTY 输入所有权的唯一消息标识。
    message_id: UUID
    #: 导致本响应的消息；主动事件为 null。
    causation_id: UUID | None
    #: 带时区的消息创建时间。
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """在跨进程边界拒绝本地或不带时区的 datetime。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime message timestamp must be timezone-aware")
        return value


class PtyInputPayload(StrictHttpModel):
    """携带有界标准 Base64 PTY 输入分块。"""

    #: 接收字节的活动 PTY 会话。
    pty_session_id: UUID
    #: 解码后恰为 1..32768 字节的标准 Base64。
    data_b64: str = Field(json_schema_extra={"contentEncoding": "base64"})

    @field_validator("data_b64")
    @classmethod
    def validate_data(cls, value: str) -> str:
        """要求标准 Base64 并遵守现有 PTY 字节上限。"""

        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("PTY input must use canonical Base64") from error
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("PTY input must use canonical Base64")
        if not 1 <= len(decoded) <= 32_768:
            raise ValueError("PTY input must contain 1..32768 bytes")
        return value

    def decoded_data(self) -> bytes:
        """严格校验器接受值后才解码字节。"""

        return base64.b64decode(self.data_b64, validate=True)


class PtyInputMessage(RuntimeMessageBase):
    """通过 Runtime WebSocket 请求带关联标识的 PTY 写入。"""

    #: PTY 输入判别字段。
    type: Literal["pty.input"]
    #: 客户端消息不能声明因果关联所有者。
    causation_id: None
    #: 严格 PTY 输入载荷。
    payload: PtyInputPayload


class RuntimePingPayload(StrictHttpModel):
    """携带桌面端显式心跳时间戳。"""

    #: 桌面端创建 ping 时的带时区时间。
    client_timestamp: datetime

    @field_validator("client_timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """拒绝不带时区的客户端心跳时间。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("client timestamp must be timezone-aware")
        return value


class RuntimePingMessage(RuntimeMessageBase):
    """仅刷新显式 Runtime 心跳状态。"""

    #: Runtime ping 判别字段。
    type: Literal["runtime.ping"]
    #: 客户端消息不能声明因果关联所有者。
    causation_id: None
    #: 严格心跳载荷。
    payload: RuntimePingPayload


RuntimeClientMessage = Annotated[
    PtyInputMessage | RuntimePingMessage,
    Field(discriminator="type"),
]


class PtyInputResultPayload(StrictHttpModel):
    """报告精确 PTY 写入结果，不因领域失败关闭连接。"""

    #: 请求的 PTY 会话标识。
    pty_session_id: UUID
    #: 已接受的解码字节数；领域拒绝写入时为零。
    accepted_bytes: Annotated[int, Field(ge=0, le=32_768, strict=True)]
    #: 稳定领域错误码；成功时为 null。
    error_code: str | None


class PtyInputResultMessage(RuntimeMessageBase):
    """关联 PTY 输入成功或稳定领域失败。"""

    #: PTY 输入结果判别字段。
    type: Literal["pty.input_result"]
    #: PTY 结果始终标识导致该结果的输入消息。
    causation_id: UUID
    #: 严格结果载荷。
    payload: PtyInputResultPayload


class PtyOutputPayload(PtyInputPayload):
    """携带单调有序的 PTY 输出分块。"""

    #: 每个 PTY 内单调递增的输出序号。
    stream_sequence: Annotated[int, Field(ge=0, strict=True)]


class PtyOutputMessage(RuntimeMessageBase):
    """发布主动推送的 PTY 输出事件。"""

    #: PTY 输出判别字段。
    type: Literal["pty.output"]
    #: 领域输出是主动推送，不由客户端消息触发。
    causation_id: None
    #: 严格输出载荷。
    payload: PtyOutputPayload


class PtyClosedPayload(StrictHttpModel):
    """保留当前 PTY 进程退出投影。"""

    #: 已关闭 PTY 会话标识。
    pty_session_id: UUID
    #: AsyncSSH 提供的远程进程退出状态。
    exit_status: int | None
    #: AsyncSSH 提供的远程进程退出信号。
    exit_signal: str | None


class PtyClosedMessage(RuntimeMessageBase):
    """发布主动推送的 PTY 关闭事件。"""

    #: PTY 关闭判别字段。
    type: Literal["pty.closed"]
    #: 领域关闭是主动事件，不由客户端消息触发。
    causation_id: None
    #: 严格关闭载荷。
    payload: PtyClosedPayload


class SshConnectionStateMessage(RuntimeMessageBase):
    """发布完整的当前安全 SSH 状态投影。"""

    #: SSH 连接状态判别字段。
    type: Literal["ssh.connection_state"]
    #: 领域 SSH 状态为主动事件。
    causation_id: None
    #: 现有安全 ConnectionStatus，不虚构有损状态。
    payload: ConnectionStatus


class SftpOperationProgressMessage(RuntimeMessageBase):
    """发布完整的当前安全手动 SFTP 进展投影。"""

    #: 手动 SFTP 操作进展判别字段。
    type: Literal["sftp.operation_progress"]
    #: 领域手动 SFTP 进展为主动事件。
    causation_id: None
    #: 现有变更进展投影。
    payload: MutationProgressProjection


class RuntimePongPayload(StrictHttpModel):
    """携带服务器带时区的心跳响应时间。"""

    #: 创建 pong 时的 UTC 时间。
    server_timestamp: datetime


class RuntimePongMessage(RuntimeMessageBase):
    """关联显式 Runtime ping。"""

    #: Runtime pong 判别字段。
    type: Literal["runtime.pong"]
    #: pong 始终标识触发它的 ping。
    causation_id: UUID
    #: 严格 pong 载荷。
    payload: RuntimePongPayload


class RuntimeErrorPayload(StrictHttpModel):
    """暴露稳定 WebSocket 领域失败，不包含原始内部信息。"""

    #: 稳定机器可读错误码。
    error_code: str
    #: 安全固定公开消息。
    message: str
    #: 错误码专属允许列表内的上下文，或 null。
    details: dict[str, JsonValue] | None


class RuntimeErrorMessage(RuntimeMessageBase):
    """关联非 PTY 领域失败，同时保留连接。"""

    #: Runtime 错误判别字段。
    type: Literal["runtime.error"]
    #: 严格安全错误载荷。
    payload: RuntimeErrorPayload


RuntimeServerMessage = Annotated[
    PtyInputResultMessage
    | PtyOutputMessage
    | PtyClosedMessage
    | SshConnectionStateMessage
    | SftpOperationProgressMessage
    | RuntimePongMessage
    | RuntimeErrorMessage,
    Field(discriminator="type"),
]


__all__ = [
    "HealthLiveResponse",
    "HealthReadyResponse",
    "JsonValue",
    "ProblemDetails",
    "RuntimeStateResponse",
    "StrictHttpModel",
    "AgentApiConfigListResponse",
    "AgentApiConfigResponse",
    "ConnectionListResponse",
    "ConnectionResponse",
    "CredentialPublicKeyResponse",
    "DeleteResponse",
    "HostKeyResponse",
    "PtySessionResponse",
    "SshStatusResponse",
    "PtyClosedMessage",
    "PtyClosedPayload",
    "PtyInputMessage",
    "PtyInputPayload",
    "PtyInputResultMessage",
    "PtyInputResultPayload",
    "PtyOutputMessage",
    "PtyOutputPayload",
    "RuntimeClientMessage",
    "RuntimeErrorMessage",
    "RuntimeErrorPayload",
    "RuntimeMessageBase",
    "RuntimePingMessage",
    "RuntimePingPayload",
    "RuntimePongMessage",
    "RuntimePongPayload",
    "RuntimeServerMessage",
    "SftpOperationProgressMessage",
    "SshConnectionStateMessage",
]
