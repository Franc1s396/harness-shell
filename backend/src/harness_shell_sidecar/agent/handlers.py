"""Strict dispatcher handlers for Agent configuration and secret turn execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Annotated, Any, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
)

from harness_shell_sidecar.credentials import (
    CredentialCipherError,
    CredentialEnvelope,
    CredentialRepository,
    CredentialRepositoryError,
    RuntimeCredentialCipher,
    zeroize,
)
from harness_shell_sidecar.runtime.dispatcher import (
    DispatchError,
    Handler,
    RequestDispatcher,
)
from harness_shell_sidecar.runtime.request_context import RequestContext
from harness_shell_sidecar.storage import RuntimeDatabase

from .api_configs import ApiConfigRepository, ApiConfigRepositoryError
from .contracts import (
    AgentTurnInput,
    AgentTurnResult,
    ModelApiConfig,
    ModelApiConfigFields,
    ModelApiConfigInput,
)
from .service import AgentServiceError


_PUBLIC_REPOSITORY_ERRORS = {
    "MODEL_API_CONFIG_NOT_FOUND": (
        "MODEL_API_CONFIG_NOT_FOUND",
        "model API configuration was not found",
    ),
    "MODEL_API_CONFIG_IN_USE": (
        "MODEL_API_CONFIG_IN_USE",
        "model API configuration is in use",
    ),
    "MODEL_API_CONFIG_PERSISTENCE_FAILED": (
        "MODEL_API_CONFIG_PERSISTENCE_FAILED",
        "model API configuration persistence failed",
    ),
}
_UNKNOWN_REPOSITORY_ERROR = (
    "MODEL_API_CONFIG_PERSISTENCE_FAILED",
    "model API configuration persistence failed",
)
_MAX_JSON_RESPONSE_BYTES = 1_048_576


class _AgentServiceProtocol(Protocol):
    """Describe the secret turn service surface required by the handler."""

    async def run_turn(
        self,
        request: AgentTurnInput,
        api_key: SecretStr,
        cancelled: asyncio.Event,
        *,
        expected_config: ModelApiConfig,
    ) -> AgentTurnResult:
        """Execute one bounded Agent turn."""


class _EmptyParams(BaseModel):
    """Reject every field for list operations."""

    model_config = ConfigDict(extra="forbid", strict=True)


class _ApiConfigIdParams(BaseModel):
    """Locate one model API configuration by opaque identity."""

    model_config = ConfigDict(extra="forbid", strict=True)

    api_config_id: UUID


class ModelApiConfigCreateRequest(ModelApiConfigFields):
    """Carry Provider fields and one current-key encrypted API key."""

    api_key_envelope: CredentialEnvelope = Field(
        description="API key encrypted for the current Runtime."
    )


class ModelApiConfigUpdateRequest(ModelApiConfigFields):
    """Carry Provider fields and an optional replacement API key."""

    api_key_envelope: CredentialEnvelope | None = Field(
        default=None,
        description="Optional replacement API key encrypted for this Runtime.",
    )


class _ApiConfigUpdateParams(ModelApiConfigUpdateRequest):
    """Combine Provider fields with identity and optional replacement API key."""

    api_config_id: UUID = Field(description="Provider configuration to replace.")


class AgentTurnRequest(BaseModel):
    """Validate the identity-only public payload accepted for an Agent turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    conversation_id: UUID | None = None
    ssh_session_id: UUID
    api_config_id: UUID
    user_message: Annotated[str, StringConstraints(min_length=1, max_length=65536)]

    def to_input(self) -> AgentTurnInput:
        """Build the non-secret Agent input after configuration checks pass."""

        return AgentTurnInput(
            conversation_id=self.conversation_id,
            ssh_session_id=self.ssh_session_id,
            api_config_id=self.api_config_id,
            user_message=self.user_message,
        )


def register_agent_handlers(
    dispatcher: RequestDispatcher,
    api_configs: ApiConfigRepository,
    agent_service: _AgentServiceProtocol,
    credential_repository: CredentialRepository,
    credential_cipher: RuntimeCredentialCipher,
    database: RuntimeDatabase,
) -> None:
    """Register aggregate Provider CRUD and identity-only Agent turns."""

    async def list_configs(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Return ordered non-secret model configuration metadata."""

        _params(raw_params, _EmptyParams)
        context.require_active()
        return {
            "configs": [config.model_dump(mode="json") for config in api_configs.list()]
        }

    async def create_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Atomically persist an API key credential and its Provider metadata."""

        params = _params(raw_params, ModelApiConfigCreateRequest)
        context.require_active()
        with database.transaction():
            credential_id = _create_api_key(
                credential_cipher,
                credential_repository,
                params.api_key_envelope,
            )
            value = _api_config_input(params, credential_id)
            created = api_configs.create(value)
        return {"config": created.model_dump(mode="json")}

    async def update_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Atomically replace Provider metadata and optionally its API key."""

        params = _params(raw_params, _ApiConfigUpdateParams)
        context.require_active()
        current = api_configs.get(params.api_config_id)
        if current is None:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        with database.transaction():
            credential_id = current.api_key_credential_id
            if params.api_key_envelope is not None:
                credential_id = _create_api_key(
                    credential_cipher,
                    credential_repository,
                    params.api_key_envelope,
                )
            value = _api_config_input(params, credential_id)
            updated = api_configs.update(params.api_config_id, value)
            if credential_id != current.api_key_credential_id:
                _delete_owned_credential(
                    credential_repository, current.api_key_credential_id
                )
        return {"config": updated.model_dump(mode="json")}

    async def delete_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Atomically delete Provider metadata and its owned API key credential."""

        params = _params(raw_params, _ApiConfigIdParams)
        context.require_active()
        current = api_configs.get(params.api_config_id)
        if current is None:
            return {"deleted": False}
        with database.transaction():
            deleted = api_configs.delete(params.api_config_id)
            if not deleted:
                raise ApiConfigRepositoryError(
                    "MODEL_API_CONFIG_PERSISTENCE_FAILED",
                    "model API configuration changed during deletion",
                )
            _delete_owned_credential(
                credential_repository, current.api_key_credential_id
            )
        return {"deleted": True}

    async def run_turn(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Resolve the configured API key, reject config races, and run one turn."""

        params = _params(raw_params, AgentTurnRequest)
        config = api_configs.get(params.api_config_id)
        if config is None:
            raise DispatchError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        if not config.enabled:
            raise DispatchError(
                "MODEL_API_CONFIG_DISABLED",
                "model API configuration is disabled",
            )
        context.require_active()

        try:
            decoded = credential_repository.resolve(
                config.api_key_credential_id,
                "api_key",
            )
        except CredentialRepositoryError as error:
            raise DispatchError(
                error.error_code,
                "Agent credential resolution failed",
            ) from None
        current_config = api_configs.get(params.api_config_id)
        if current_config != config:
            zeroize(decoded)
            raise DispatchError(
                "MODEL_API_CONFIG_CHANGED",
                "model API configuration changed before turn dispatch",
            )
        api_key_text = ""
        api_key: SecretStr | None = None
        try:
            try:
                api_key_text = decoded.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise DispatchError(
                    "INVALID_REQUEST_PAYLOAD",
                    "API key must be valid UTF-8",
                ) from error
            api_key = SecretStr(api_key_text)
            request = params.to_input()
            result = await agent_service.run_turn(
                request,
                api_key,
                context.cancelled,
                expected_config=config,
            )
            payload = result.model_dump(mode="json")
            _require_agent_response_fits(payload)
            return payload
        except AgentServiceError as error:
            raise DispatchError(
                error.error_code,
                "Agent turn could not be started",
            ) from None
        except DispatchError:
            raise
        except Exception:
            raise DispatchError(
                "AGENT_TURN_FAILED",
                "Agent turn failed",
            ) from None
        finally:
            api_key = None
            api_key_text = ""
            zeroize(decoded)

    handlers = {
        "agent.api_configs.list": list_configs,
        "agent.api_configs.create": create_config,
        "agent.api_configs.update": update_config,
        "agent.api_configs.delete": delete_config,
        "agent.turn.run": run_turn,
    }
    for method, handler in handlers.items():
        dispatcher.register(method, _map_repository_errors(handler))


def _params(raw_params: Mapping[str, object], model: type[BaseModel]) -> Any:
    """Validate JSON params strictly while permitting canonical UUID JSON strings."""

    if not isinstance(raw_params, Mapping):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "request params must be an object",
        )
    try:
        return model.model_validate_json(json.dumps(dict(raw_params)))
    except (TypeError, ValueError, ValidationError) as error:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "request params are invalid",
        ) from error


def _require_agent_response_fits(payload: dict[str, object]) -> None:
    """Reject a result which cannot fit in one bounded JSON response.

    AgentService performs the authoritative pre-COMPLETED budget check. This
    handler check is defense in depth and must never truncate, summarize, or
    move final text to an implicit fallback.
    """

    encoded_size = len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if encoded_size > _MAX_JSON_RESPONSE_BYTES:
        raise DispatchError(
            "AGENT_RESPONSE_TOO_LARGE",
            "Agent response exceeds the typed HTTP JSON body limit",
        )


def _map_repository_errors(handler: Handler) -> Handler:
    """Convert only stable repository failures into public dispatch errors."""

    async def wrapped(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Invoke one handler and redact repository codes and diagnostics."""

        try:
            return await handler(context, raw_params)
        except ApiConfigRepositoryError as error:
            error_code, message = _PUBLIC_REPOSITORY_ERRORS.get(
                error.error_code,
                _UNKNOWN_REPOSITORY_ERROR,
            )
            raise DispatchError(error_code, message) from None
        except (CredentialCipherError, CredentialRepositoryError) as error:
            raise DispatchError(error.error_code, "credential mutation failed") from None

    return wrapped


def _create_api_key(
    cipher: RuntimeCredentialCipher,
    repository: CredentialRepository,
    envelope: CredentialEnvelope,
) -> UUID:
    """Decrypt, persist, and promptly clear one Provider API key buffer."""

    secret = cipher.decrypt(envelope)
    text = ""
    try:
        try:
            text = secret.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CredentialRepositoryError("CREDENTIAL_SECRET_INVALID") from None
        return repository.create("api_key", text)
    finally:
        text = ""
        zeroize(secret)


def _api_config_input(
    params: ModelApiConfigCreateRequest | _ApiConfigUpdateParams,
    credential_id: UUID,
) -> ModelApiConfigInput:
    """Build the repository value after the aggregate API key exists."""

    return ModelApiConfigInput(
        display_name=params.display_name,
        api_type=params.api_type,
        base_url=params.base_url,
        model=params.model,
        api_key_credential_id=credential_id,
        enabled=params.enabled,
    )


def _delete_owned_credential(
    repository: CredentialRepository,
    credential_id: UUID,
) -> None:
    """Delete an aggregate-owned credential or expose durable state divergence."""

    if not repository.delete(credential_id):
        raise CredentialRepositoryError("CREDENTIAL_NOT_FOUND")
