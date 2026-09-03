"""Validated dispatcher handlers for direct SSH lifecycle operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from harness_shell_sidecar.credentials import (
    CredentialService,
    CredentialServiceError,
)
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
    dispatcher: RequestDispatcher,
    runtime: _SshRuntimeProtocol,
    credential_service: CredentialService,
) -> None:
    """Register identity-only SSH operations with internal secret resolution."""

    async def inspect_host_key(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        params = _params(raw_params, HostKeyInspectionRequest)
        context.require_active()
        resolved = None
        try:
            resolved = credential_service.build_ssh_connect(params.connection_id)
            status = await runtime.inspect_host_key(
                params.connection_id,
                jump_connection_id=resolved.jump_connection_id,
                jump_password=resolved.jump_password,
                jump_private_key=resolved.jump_private_key,
                jump_passphrase=resolved.jump_passphrase,
                expected_jump_profile_version=resolved.jump_profile_version,
            )
        except CredentialServiceError as exc:
            raise DispatchError(
                exc.error_code,
                "SSH credential resolution failed",
            ) from None
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        finally:
            if resolved is not None:
                resolved.close()
        return {"status": status.model_dump(mode="json")}

    async def connect(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        params = _params(raw_params, SshConnectRequest)
        context.require_active()
        resolved = None
        try:
            resolved = credential_service.build_ssh_connect(params.connection_id)
            status = await runtime.connect(
                params.connection_id,
                password=resolved.password,
                private_key=resolved.private_key,
                passphrase=resolved.passphrase,
                expected_profile_version=resolved.profile_version,
                jump_connection_id=resolved.jump_connection_id,
                jump_password=resolved.jump_password,
                jump_private_key=resolved.jump_private_key,
                jump_passphrase=resolved.jump_passphrase,
                expected_jump_profile_version=resolved.jump_profile_version,
            )
            return {"status": status.model_dump(mode="json")}
        except CredentialServiceError as exc:
            raise DispatchError(
                exc.error_code,
                "SSH credential resolution failed",
            ) from None
        except SshRuntimeError as exc:
            raise _dispatch_error(exc) from exc
        finally:
            if resolved is not None:
                resolved.close()

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


def _dispatch_error(error: SshRuntimeError) -> DispatchError:
    details = error.public_payload()
    details.pop("error_code", None)
    return DispatchError(error.error_code, "SSH operation failed", details=details)
