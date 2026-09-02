"""Validated dispatcher handlers for direct SSH lifecycle operations."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext

from .errors import ConnectionStatus, SshRuntimeError
from .models import (
    HostKeyInspectionRequest,
    SshConnectRequest,
    SshSessionRequest,
)


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


def register_ssh_handlers(
    dispatcher: RequestDispatcher, runtime: _SshRuntimeProtocol
) -> None:
    async def inspect_host_key(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        params = _params(raw_params, HostKeyInspectionRequest)
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
            context.require_active()
            status = await runtime.inspect_host_key(
                params.connection_id,
                jump_connection_id=(
                    None if params.jump is None else params.jump.connection_id
                ),
                jump_password=jump_password,
                jump_private_key=jump_private_key,
                jump_passphrase=jump_passphrase,
                expected_jump_profile_version=(
                    None if params.jump is None else params.jump.profile_version
                ),
            )
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        finally:
            for secret in secrets:
                _zeroize(secret)
        return {"status": status.model_dump(mode="json")}

    async def connect(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        params = _params(raw_params, SshConnectRequest)
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
            context.require_active()
            status = await runtime.connect(
                params.connection_id,
                password=password,
                private_key=private_key,
                passphrase=passphrase,
                expected_profile_version=params.profile_version,
                jump_connection_id=(
                    None if params.jump is None else params.jump.connection_id
                ),
                jump_password=jump_password,
                jump_private_key=jump_private_key,
                jump_passphrase=jump_passphrase,
                expected_jump_profile_version=(
                    None if params.jump is None else params.jump.profile_version
                ),
            )
            return {"status": status.model_dump(mode="json")}
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        finally:
            for secret in secrets:
                _zeroize(secret)

    async def disconnect(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        params = _params(raw_params, SshSessionRequest)
        context.require_active()
        try:
            status = await runtime.disconnect(params.ssh_session_id)
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        return {"status": status.model_dump(mode="json")}

    dispatcher.register("host_key.inspect", inspect_host_key)
    dispatcher.register("ssh.connect", connect)
    dispatcher.register("ssh.disconnect", disconnect)


def _params(raw_params: Mapping[str, object], model: type[BaseModel]):
    if not isinstance(raw_params, Mapping):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        return model.model_validate_json(json.dumps(dict(raw_params)))
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


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
