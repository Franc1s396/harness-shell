"""Validated dispatcher handlers for connection profile and Host Key storage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from harness_shell_sidecar.credentials import (
    CredentialCipherError,
    CredentialEnvelope,
    CredentialKind,
    CredentialRepository,
    CredentialRepositoryError,
    RuntimeCredentialCipher,
    zeroize,
)
from harness_shell_sidecar.runtime.dispatcher import DispatchError, Handler, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext
from harness_shell_sidecar.storage import RuntimeDatabase

from .models import ConnectionProfileFields, ConnectionProfileInput, HostKeyCandidate
from .repository import ConnectionRepository, ConnectionRepositoryError


class _EmptyParams(BaseModel):
    """表示不接受任何业务参数的严格请求体。"""

    #: 拒绝调用方传入任何未声明参数。
    model_config = ConfigDict(extra="forbid", strict=True)


class _ConnectionIdParams(BaseModel):
    """仅按连接标识符定位连接的请求参数。"""

    #: 对 IPC 请求参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 目标连接配置的唯一标识符。
    connection_id: UUID


class ConnectionCreateRequest(ConnectionProfileFields):
    """携带新连接字段及由当前 Runtime 公钥加密的认证秘密。"""

    credential_envelope: CredentialEnvelope = Field(
        description="Authentication secret encrypted for the current Runtime."
    )
    passphrase_envelope: CredentialEnvelope | None = Field(
        default=None,
        description="Optional private-key passphrase encrypted for this Runtime.",
    )

    @model_validator(mode="after")
    def validate_passphrase(self) -> ConnectionCreateRequest:
        """禁止密码认证携带仅属于私钥的口令。"""

        if self.auth_kind == "password" and self.passphrase_envelope is not None:
            raise ValueError("password authentication cannot use a private-key passphrase")
        return self


class ConnectionUpdateRequest(ConnectionProfileFields):
    """携带完整连接字段以及可选的替换认证秘密。"""

    #: 省略时保留当前认证秘密；认证类型变化时必须提供。
    credential_envelope: CredentialEnvelope | None = Field(
        default=None,
        description="Optional replacement authentication secret.",
    )
    #: 省略时保留当前私钥口令；切换到密码认证时必须为空。
    passphrase_envelope: CredentialEnvelope | None = Field(
        default=None,
        description="Optional replacement private-key passphrase.",
    )

    @model_validator(mode="after")
    def validate_passphrase(self) -> ConnectionUpdateRequest:
        """禁止密码认证携带仅属于私钥的口令。"""

        if self.auth_kind == "password" and self.passphrase_envelope is not None:
            raise ValueError("password authentication cannot use a private-key passphrase")
        return self


class _ConnectionUpdateParams(ConnectionUpdateRequest):
    """Combine one target identity with a validated connection update request."""

    connection_id: UUID = Field(description="Connection profile to replace.")


class _HostKeyReplaceParams(HostKeyCandidate):
    """替换 Host Key 时携带的候选值与并发保护条件。"""

    #: 调用方预期当前仍生效的旧指纹，用于避免覆盖并发变化。
    expected_old_fingerprint: str


def register_connection_handlers(
    dispatcher: RequestDispatcher,
    repository: ConnectionRepository,
    credential_repository: CredentialRepository,
    credential_cipher: RuntimeCredentialCipher,
    database: RuntimeDatabase,
) -> None:
    """Register connection operations with aggregate credential ownership."""

    async def list_connections(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        """Return all persisted connection profiles in repository order."""

        _params(raw_params, _EmptyParams)
        context.require_active()
        return {
            "connections": [
                profile.model_dump(mode="json") for profile in repository.list()
            ]
        }

    async def create_connection(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        """Atomically create owned credentials and their connection profile."""

        params = _params(raw_params, ConnectionCreateRequest)
        context.require_active()
        with database.transaction():
            credential_id = _create_credential(
                credential_cipher,
                credential_repository,
                params.credential_envelope,
                _credential_kind(params.auth_kind),
            )
            passphrase_id = (
                None
                if params.passphrase_envelope is None
                else _create_credential(
                    credential_cipher,
                    credential_repository,
                    params.passphrase_envelope,
                    "private_key_passphrase",
                )
            )
            value = _connection_profile_input(params, credential_id, passphrase_id)
            created = repository.create(value)
        return {"connection": created.model_dump(mode="json")}

    async def update_connection(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        """Atomically replace a profile and any explicitly supplied credentials."""

        params = _params(raw_params, _ConnectionUpdateParams)
        context.require_active()
        current = repository.get(params.connection_id)
        if current is None:
            raise ConnectionRepositoryError(
                "CONNECTION_NOT_FOUND", "connection profile was not found"
            )
        if current.auth_kind != params.auth_kind and params.credential_envelope is None:
            raise DispatchError(
                "INVALID_REQUEST_PAYLOAD",
                "authentication changes require a new credential",
            )
        with database.transaction():
            credential_id = current.credential_id
            if params.credential_envelope is not None:
                credential_id = _create_credential(
                    credential_cipher,
                    credential_repository,
                    params.credential_envelope,
                    _credential_kind(params.auth_kind),
                )
            passphrase_id = _updated_passphrase_id(
                params,
                current.auth_kind,
                current.passphrase_credential_id,
                credential_cipher,
                credential_repository,
            )
            value = _connection_profile_input(params, credential_id, passphrase_id)
            updated = repository.update(params.connection_id, value)
            _delete_replaced_credentials(
                credential_repository,
                (current.credential_id, current.passphrase_credential_id),
                (credential_id, passphrase_id),
            )
        return {"connection": updated.model_dump(mode="json")}

    async def delete_connection(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        """Atomically delete a profile and every credential it owns."""

        params = _params(raw_params, _ConnectionIdParams)
        context.require_active()
        current = repository.get(params.connection_id)
        if current is None:
            return {"deleted": False}
        with database.transaction():
            deleted = repository.delete(params.connection_id)
            if not deleted:
                raise ConnectionRepositoryError(
                    "CONNECTION_PERSISTENCE_FAILED",
                    "connection profile changed during deletion",
                )
            _delete_replaced_credentials(
                credential_repository,
                (current.credential_id, current.passphrase_credential_id),
                (),
            )
        return {"deleted": True}

    async def confirm_host_key(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        """Persist the first explicitly confirmed Host Key for a connection."""

        candidate = _params(raw_params, HostKeyCandidate)
        context.require_active()
        return {
            "host_key": repository.trust_first_host_key(candidate).model_dump(
                mode="json"
            )
        }

    async def replace_host_key(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        """Replace the trusted Host Key under optimistic fingerprint control."""

        params = _params(raw_params, _HostKeyReplaceParams)
        context.require_active()
        candidate = HostKeyCandidate.model_validate(
            params.model_dump(exclude={"expected_old_fingerprint"})
        )
        return {
            "host_key": repository.replace_host_key(
                candidate, params.expected_old_fingerprint
            ).model_dump(mode="json")
        }

    handlers = {
        "connections.list": list_connections,
        "connections.create": create_connection,
        "connections.update": update_connection,
        "connections.delete": delete_connection,
        "host_key.confirm": confirm_host_key,
        "host_key.replace": replace_host_key,
    }
    for method, handler in handlers.items():
        dispatcher.register(method, _map_repository_errors(handler))


def _params(raw_params: Mapping[str, object], model: type[BaseModel]) -> Any:
    """Validate JSON params strictly while permitting canonical UUID strings."""

    if not isinstance(raw_params, Mapping):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        # JSON-mode validation preserves strict UUID parsing from JSON strings.
        return model.model_validate_json(json.dumps(dict(raw_params)))
    except (TypeError, ValueError, ValidationError) as exc:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params are invalid"
        ) from exc


def _map_repository_errors(handler: Handler) -> Handler:
    """Map connection and aggregate credential failures to stable dispatch errors."""

    async def wrapped(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Invoke one connection handler and retain only stable failure codes."""

        try:
            return await handler(context, raw_params)
        except ConnectionRepositoryError as exc:
            raise DispatchError(exc.error_code, str(exc)) from exc
        except (CredentialCipherError, CredentialRepositoryError) as exc:
            raise DispatchError(exc.error_code, "credential mutation failed") from None

    return wrapped


def _credential_kind(auth_kind: str) -> CredentialKind:
    """Map the closed connection authentication kind to its credential purpose."""

    return "ssh_password" if auth_kind == "password" else "imported_private_key"


def _create_credential(
    cipher: RuntimeCredentialCipher,
    repository: CredentialRepository,
    envelope: CredentialEnvelope,
    kind: CredentialKind,
) -> UUID:
    """Decrypt, persist, and promptly clear one aggregate credential buffer."""

    secret = cipher.decrypt(envelope)
    text = ""
    try:
        try:
            text = secret.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CredentialRepositoryError(
                "CREDENTIAL_SECRET_INVALID",
                "the decrypted connection credential is not valid UTF-8",
            ) from None
        return repository.create(kind, text)
    finally:
        text = ""
        zeroize(secret)


def _connection_profile_input(
    params: ConnectionCreateRequest | _ConnectionUpdateParams,
    credential_id: UUID,
    passphrase_id: UUID | None,
) -> ConnectionProfileInput:
    """Build the repository value after aggregate-owned credentials exist."""

    return ConnectionProfileInput(
        display_name=params.display_name,
        group_name=params.group_name,
        host=params.host,
        port=params.port,
        username=params.username,
        auth_kind=params.auth_kind,
        credential_id=credential_id,
        passphrase_credential_id=passphrase_id,
        proxy_jump_id=params.proxy_jump_id,
        favorite=params.favorite,
    )


def _updated_passphrase_id(
    params: _ConnectionUpdateParams,
    current_auth_kind: str,
    current_passphrase_id: UUID | None,
    cipher: RuntimeCredentialCipher,
    repository: CredentialRepository,
) -> UUID | None:
    """Select, replace, or drop the connection-owned private-key passphrase."""

    if params.auth_kind == "password":
        return None
    if params.passphrase_envelope is not None:
        return _create_credential(
            cipher,
            repository,
            params.passphrase_envelope,
            "private_key_passphrase",
        )
    if current_auth_kind == "private_key":
        return current_passphrase_id
    return None


def _delete_replaced_credentials(
    repository: CredentialRepository,
    previous_ids: tuple[UUID | None, ...],
    retained_ids: tuple[UUID | None, ...],
) -> None:
    """Delete no-longer-owned credentials and fail if durable ownership diverged."""

    retained = {credential_id for credential_id in retained_ids if credential_id is not None}
    for credential_id in {
        value for value in previous_ids if value is not None and value not in retained
    }:
        if not repository.delete(credential_id):
            raise CredentialRepositoryError(
                "CREDENTIAL_NOT_FOUND",
                "an obsolete connection credential could not be deleted",
            )
