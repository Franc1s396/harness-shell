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
    async def inspect_host_key(
        self, connection_id: UUID, **secrets
    ) -> ConnectionStatus: ...

    async def connect(self, connection_id: UUID, **secrets) -> ConnectionStatus: ...

    async def disconnect(self, session_id: UUID) -> ConnectionStatus: ...


class _AuthenticationParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    password_b64: str | None = None
    private_key_b64: str | None = None
    passphrase_b64: str | None = None

    @model_validator(mode="after")
    def require_one_authentication_kind(self) -> _AuthenticationParams:
        if (self.password_b64 is None) == (self.private_key_b64 is None):
            raise ValueError("exactly one authentication secret is required")
        if self.private_key_b64 is None and self.passphrase_b64 is not None:
            raise ValueError("passphrase requires a private key")
        return self


class _JumpAuthenticationParams(_AuthenticationParams):
    connection_id: UUID
    profile_updated_at: AwareDatetime


class _InspectParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_id: UUID
    jump: _JumpAuthenticationParams | None = None


class _SessionIdParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ssh_session_id: UUID


class _ConnectParams(_AuthenticationParams):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_id: UUID
    profile_updated_at: AwareDatetime
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
