"""Validated dispatcher handlers for direct SSH lifecycle operations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Protocol
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    ValidationError,
    model_validator,
)

from harness_shell_sidecar.protocol import FrameEnvelope, Sensitivity
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher

from .errors import ConnectionStatus, SshRuntimeError


class _SshRuntimeProtocol(Protocol):
    """SSH dispatcher handlers 依赖的最小运行时接口。"""

    async def inspect_host_key(
        self, connection_id: UUID, **secrets
    ) -> ConnectionStatus:
        """检查目标端点 Host Key，但不建立可复用登录会话。"""

        ...

    async def connect(self, connection_id: UUID, **secrets) -> ConnectionStatus:
        """使用瞬时凭据建立并注册 SSH 会话。"""

        ...

    async def disconnect(self, session_id: UUID) -> ConnectionStatus:
        """关闭指定 SSH 会话及其全部子 channel。"""

        ...


class _AuthenticationParams(BaseModel):
    """IPC 请求中互斥的 Base64 认证秘密字段。"""

    #: 拒绝额外字段和类型隐式转换。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 密码认证秘密的标准 Base64 编码。
    password_b64: str | None = None
    #: 私钥文件字节的标准 Base64 编码。
    private_key_b64: str | None = None
    #: 私钥口令字节的标准 Base64 编码。
    passphrase_b64: str | None = None

    @model_validator(mode="after")
    def require_one_authentication_kind(self) -> _AuthenticationParams:
        """要求密码与私钥二选一，并限制口令只能配合私钥。"""

        if (self.password_b64 is None) == (self.private_key_b64 is None):
            raise ValueError("exactly one authentication secret is required")
        if self.private_key_b64 is None and self.passphrase_b64 is not None:
            raise ValueError("passphrase requires a private key")
        return self


class _JumpAuthenticationParams(_AuthenticationParams):
    """ProxyJump 连接的身份、版本快照与认证秘密。"""

    #: 跳板连接配置标识符。
    connection_id: UUID
    #: 调用方读取跳板配置时看到的更新时间，用于拒绝陈旧秘密。
    profile_updated_at: AwareDatetime


class _InspectParams(BaseModel):
    """Host Key 检查请求参数及可选跳板认证。"""

    #: 对 IPC 参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 要检查 Host Key 的目标连接配置标识符。
    connection_id: UUID
    #: 通过 ProxyJump 检查时提供的跳板瞬时认证信息。
    jump: _JumpAuthenticationParams | None = None


class _SessionIdParams(BaseModel):
    """仅定位一个活动 SSH 会话的请求参数。"""

    #: 对 IPC 参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 要断开的 SSH 会话标识符。
    ssh_session_id: UUID


class _ConnectParams(_AuthenticationParams):
    """目标连接的版本快照、认证秘密及可选跳板信息。"""

    #: 明确保持严格参数校验配置。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 要建立会话的目标连接配置标识符。
    connection_id: UUID
    #: 调用方读取目标配置时看到的更新时间，用于拒绝陈旧秘密。
    profile_updated_at: AwareDatetime
    #: 可选的 ProxyJump 连接及其瞬时认证信息。
    jump: _JumpAuthenticationParams | None = None


def register_ssh_handlers(
    dispatcher: RequestDispatcher, runtime: _SshRuntimeProtocol
) -> None:
    async def inspect_host_key(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _InspectParams)
        if params.jump is not None and frame.sensitivity is not Sensitivity.SECRET:
            raise DispatchError(
                "SENSITIVE_FRAME_REQUIRED",
                "proxied host-key inspection requires a secret frame",
            )
        secrets: list[bytearray] = []
        try:
            jump_password = _decode_secret(
                None if params.jump is None else params.jump.password_b64, secrets
            )
            jump_private_key = _decode_secret(
                None if params.jump is None else params.jump.private_key_b64,
                secrets,
            )
            jump_passphrase = _decode_secret(
                None if params.jump is None else params.jump.passphrase_b64,
                secrets,
            )
            _require_active(cancelled)
            status = await runtime.inspect_host_key(
                params.connection_id,
                jump_connection_id=(
                    None if params.jump is None else params.jump.connection_id
                ),
                jump_password=jump_password,
                jump_private_key=jump_private_key,
                jump_passphrase=jump_passphrase,
                expected_jump_profile_updated_at=(
                    None if params.jump is None else params.jump.profile_updated_at
                ),
            )
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        finally:
            for secret in secrets:
                _zeroize(secret)
        return {"status": status.model_dump(mode="json")}

    async def connect(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        if frame.sensitivity is not Sensitivity.SECRET:
            raise DispatchError(
                "SENSITIVE_FRAME_REQUIRED", "ssh.connect requires a secret frame"
            )
        params = _params(frame, _ConnectParams)
        secrets: list[bytearray] = []
        try:
            password = _decode_secret(params.password_b64, secrets)
            private_key = _decode_secret(params.private_key_b64, secrets)
            passphrase = _decode_secret(params.passphrase_b64, secrets)
            jump_password = _decode_secret(
                None if params.jump is None else params.jump.password_b64, secrets
            )
            jump_private_key = _decode_secret(
                None if params.jump is None else params.jump.private_key_b64,
                secrets,
            )
            jump_passphrase = _decode_secret(
                None if params.jump is None else params.jump.passphrase_b64,
                secrets,
            )
            _require_active(cancelled)
            status = await runtime.connect(
                params.connection_id,
                password=password,
                private_key=private_key,
                passphrase=passphrase,
                expected_profile_updated_at=params.profile_updated_at,
                jump_connection_id=(
                    None if params.jump is None else params.jump.connection_id
                ),
                jump_password=jump_password,
                jump_private_key=jump_private_key,
                jump_passphrase=jump_passphrase,
                expected_jump_profile_updated_at=(
                    None if params.jump is None else params.jump.profile_updated_at
                ),
            )
            return {"status": status.model_dump(mode="json")}
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        finally:
            for secret in secrets:
                _zeroize(secret)

    async def disconnect(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _SessionIdParams)
        _require_active(cancelled)
        try:
            status = await runtime.disconnect(params.ssh_session_id)
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        return {"status": status.model_dump(mode="json")}

    dispatcher.register("host_key.inspect", inspect_host_key)
    dispatcher.register("ssh.connect", connect)
    dispatcher.register("ssh.disconnect", disconnect)


def _params(frame: FrameEnvelope, model: type[BaseModel]):
    params = frame.payload.get("params")
    if not isinstance(params, dict):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        return model.model_validate_json(json.dumps(params))
    except (TypeError, ValueError, ValidationError) as exc:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params are invalid"
        ) from exc


def _decode_secret(
    encoded: str | None, allocated: list[bytearray]
) -> bytearray | None:
    if encoded is None:
        return None
    try:
        decoded = bytearray(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error) as exc:
        raise DispatchError(
            "INVALID_SECRET_ENCODING", "secret must use canonical base64"
        ) from exc
    allocated.append(decoded)
    if not decoded or base64.b64encode(decoded).decode("ascii") != encoded:
        raise DispatchError(
            "INVALID_SECRET_ENCODING", "secret must use canonical base64"
        )
    return decoded


def _dispatch_error(error: SshRuntimeError) -> DispatchError:
    details = error.public_payload()
    details.pop("error_code", None)
    return DispatchError(error.error_code, "SSH operation failed", details=details)


def _require_active(cancelled: asyncio.Event) -> None:
    if cancelled.is_set():
        raise DispatchError("REQUEST_CANCELLED", "request was cancelled")


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
