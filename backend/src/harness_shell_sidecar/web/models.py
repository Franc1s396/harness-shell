"""Strict shared HTTP request and response models."""

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
    AgentTurnResult,
    ModelApiConfig,
)
from harness_shell_sidecar.connections.models import (
    ConnectionProfile,
    HostKeyRecord,
)
from harness_shell_sidecar.manual_sftp.models import MutationProgressProjection
from harness_shell_sidecar.runtime.models import (
    RuntimeInitializeRequest,
    RuntimePhase,
)
from harness_shell_sidecar.ssh.errors import ConnectionStatus
from harness_shell_sidecar.ssh.models import SshSessionSnapshot
from harness_shell_sidecar.terminal.models import PtySession


class StrictHttpModel(BaseModel):
    """Forbid undeclared HTTP fields and implicit Python coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ProblemDetails(StrictHttpModel):
    """Expose one bounded machine-readable failure without raw internals."""

    #: Stable URN for the error category.
    type: str
    #: Short safe human-readable category label.
    title: str
    #: HTTP status duplicated for strict cross-layer validation.
    status: int
    #: Stable machine-readable business or transport error code.
    error_code: str
    #: Safe bounded message that is never a client decision source.
    message: str
    #: Correlation identifier echoed in the response header.
    request_id: UUID
    #: Error-code-specific allowlisted structured context.
    details: dict[str, JsonValue]


class HealthLiveResponse(StrictHttpModel):
    """Report that the Python HTTP event loop can respond."""

    request_id: UUID
    live: bool


class HealthReadyResponse(StrictHttpModel):
    """Report that the complete runtime resource graph is ready."""

    request_id: UUID
    ready: bool
    state: RuntimePhase


class RuntimeStateResponse(StrictHttpModel):
    """Return the safe shared runtime lifecycle state."""

    request_id: UUID
    state: RuntimePhase


class RequestCancelResponse(StrictHttpModel):
    """Acknowledge an explicit cooperative cancellation request."""

    request_id: UUID
    target_request_id: UUID
    cancellation_requested: bool


class ConnectionListResponse(StrictHttpModel):
    """Return all persisted connection profiles in repository order."""

    request_id: UUID
    connections: list[ConnectionProfile]


class ConnectionResponse(StrictHttpModel):
    """Return one persisted connection profile."""

    request_id: UUID
    connection: ConnectionProfile


class DeleteResponse(StrictHttpModel):
    """Return the determined deletion outcome for an identity."""

    request_id: UUID
    deleted: bool


class HostKeyResponse(StrictHttpModel):
    """Return one persisted Host Key trust record."""

    request_id: UUID
    host_key: HostKeyRecord


class SshStatusResponse(StrictHttpModel):
    """Return one safe SSH lifecycle status."""

    request_id: UUID
    status: ConnectionStatus


class SshSessionListResponse(StrictHttpModel):
    """Return only safe metadata for active SSH sessions."""

    request_id: UUID
    sessions: list[SshSessionSnapshot]


class PtySessionResponse(StrictHttpModel):
    """Return one interactive PTY session snapshot."""

    request_id: UUID
    pty_session: PtySession


class AgentApiConfigListResponse(StrictHttpModel):
    """Return all non-secret Provider configurations."""

    request_id: UUID
    configs: list[ModelApiConfig]


class AgentApiConfigResponse(StrictHttpModel):
    """Return one non-secret Provider configuration."""

    request_id: UUID
    config: ModelApiConfig


class AgentTurnResponse(AgentTurnResult):
    """Return the complete non-streaming Agent result with HTTP correlation."""

    request_id: UUID


class RuntimeMessageBase(StrictHttpModel):
    """Carry fields shared by every strict Runtime WebSocket message."""

    #: Frozen WebSocket schema version.
    schema_version: Literal[1] = 1
    #: Unique message identity used for causation and PTY input ownership.
    message_id: UUID
    #: Message that caused this response, or null for unsolicited events.
    causation_id: UUID | None
    #: Timezone-aware message creation time.
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Reject local or naive datetimes at the cross-process boundary."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime message timestamp must be timezone-aware")
        return value


class PtyInputPayload(StrictHttpModel):
    """Carry one bounded canonical Base64 PTY input chunk."""

    #: Active PTY session that receives the bytes.
    pty_session_id: UUID
    #: Canonical Base64 for exactly 1..32768 decoded bytes.
    data_b64: str = Field(json_schema_extra={"contentEncoding": "base64"})

    @field_validator("data_b64")
    @classmethod
    def validate_data(cls, value: str) -> str:
        """Require canonical Base64 and the existing PTY byte limit."""

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
        """Decode bytes only after the strict validator has accepted the value."""

        return base64.b64decode(self.data_b64, validate=True)


class PtyInputMessage(RuntimeMessageBase):
    """Request one correlated PTY write over the Runtime WebSocket."""

    #: Discriminator for PTY input.
    type: Literal["pty.input"]
    #: Client messages cannot claim a causation owner.
    causation_id: None
    #: Strict PTY input payload.
    payload: PtyInputPayload


class RuntimePingPayload(StrictHttpModel):
    """Carry the Desktop's explicit heartbeat timestamp."""

    #: Timezone-aware time at which Desktop created the ping.
    client_timestamp: datetime

    @field_validator("client_timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Reject a naive client heartbeat time."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("client timestamp must be timezone-aware")
        return value


class RuntimePingMessage(RuntimeMessageBase):
    """Refresh only the explicit Runtime heartbeat owner."""

    #: Discriminator for Runtime ping.
    type: Literal["runtime.ping"]
    #: Client messages cannot claim a causation owner.
    causation_id: None
    #: Strict heartbeat payload.
    payload: RuntimePingPayload


RuntimeClientMessage = Annotated[
    PtyInputMessage | RuntimePingMessage,
    Field(discriminator="type"),
]


class PtyInputResultPayload(StrictHttpModel):
    """Report the exact PTY write result without closing on domain failure."""

    #: Requested PTY session identity.
    pty_session_id: UUID
    #: Accepted decoded bytes, or zero when the domain rejected the write.
    accepted_bytes: Annotated[int, Field(ge=0, le=32_768, strict=True)]
    #: Stable domain error code, or null on success.
    error_code: str | None


class PtyInputResultMessage(RuntimeMessageBase):
    """Correlate one PTY input success or stable domain failure."""

    #: Discriminator for PTY input result.
    type: Literal["pty.input_result"]
    #: PTY results always identify the input message that caused them.
    causation_id: UUID
    #: Strict result payload.
    payload: PtyInputResultPayload


class PtyOutputPayload(PtyInputPayload):
    """Carry one monotonically ordered PTY output chunk."""

    #: Per-PTY monotonically increasing output sequence.
    stream_sequence: Annotated[int, Field(ge=0, strict=True)]


class PtyOutputMessage(RuntimeMessageBase):
    """Publish one unsolicited PTY output event."""

    #: Discriminator for PTY output.
    type: Literal["pty.output"]
    #: Domain output is unsolicited rather than caused by a client message.
    causation_id: None
    #: Strict output payload.
    payload: PtyOutputPayload


class PtyClosedPayload(StrictHttpModel):
    """Preserve the current PTY process exit projection."""

    #: Closed PTY session identity.
    pty_session_id: UUID
    #: Remote process exit status when supplied by AsyncSSH.
    exit_status: int | None
    #: Remote process exit signal when supplied by AsyncSSH.
    exit_signal: str | None


class PtyClosedMessage(RuntimeMessageBase):
    """Publish one unsolicited PTY closure event."""

    #: Discriminator for PTY closure.
    type: Literal["pty.closed"]
    #: Domain closure is unsolicited rather than caused by a client message.
    causation_id: None
    #: Strict closure payload.
    payload: PtyClosedPayload


class SshConnectionStateMessage(RuntimeMessageBase):
    """Publish the complete current safe SSH status projection."""

    #: Discriminator for SSH connection state.
    type: Literal["ssh.connection_state"]
    #: Domain SSH state is unsolicited.
    causation_id: None
    #: Existing safe ConnectionStatus without lossy state invention.
    payload: ConnectionStatus


class SftpOperationProgressMessage(RuntimeMessageBase):
    """Publish the complete current safe Manual SFTP progress projection."""

    #: Discriminator for Manual SFTP operation progress.
    type: Literal["sftp.operation_progress"]
    #: Domain Manual SFTP progress is unsolicited.
    causation_id: None
    #: Existing mutation progress projection.
    payload: MutationProgressProjection


class RuntimePongPayload(StrictHttpModel):
    """Carry the server's timezone-aware heartbeat response time."""

    #: UTC time at which the pong was created.
    server_timestamp: datetime


class RuntimePongMessage(RuntimeMessageBase):
    """Correlate one explicit Runtime ping."""

    #: Discriminator for Runtime pong.
    type: Literal["runtime.pong"]
    #: Pong always identifies the ping that caused it.
    causation_id: UUID
    #: Strict pong payload.
    payload: RuntimePongPayload


class RuntimeErrorPayload(StrictHttpModel):
    """Expose one stable WebSocket domain failure without raw internals."""

    #: Stable machine-readable error code.
    error_code: str
    #: Safe fixed public message.
    message: str
    #: Error-code-owned allowlisted context, or null.
    details: dict[str, JsonValue] | None


class RuntimeErrorMessage(RuntimeMessageBase):
    """Correlate a non-PTY domain failure while retaining the connection."""

    #: Discriminator for Runtime error.
    type: Literal["runtime.error"]
    #: Strict safe error payload.
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
    "RequestCancelResponse",
    "RuntimeInitializeRequest",
    "RuntimeStateResponse",
    "StrictHttpModel",
    "AgentApiConfigListResponse",
    "AgentApiConfigResponse",
    "AgentTurnResponse",
    "ConnectionListResponse",
    "ConnectionResponse",
    "DeleteResponse",
    "HostKeyResponse",
    "PtySessionResponse",
    "SshSessionListResponse",
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
