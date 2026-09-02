"""Strict dispatcher handlers for Agent configuration and secret turn execution."""

from __future__ import annotations

import asyncio
import base64
import binascii
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
    field_validator,
)

from harness_shell_sidecar.runtime.dispatcher import (
    DispatchError,
    Handler,
    RequestDispatcher,
)
from harness_shell_sidecar.runtime.request_context import RequestContext

from .api_configs import ApiConfigRepository, ApiConfigRepositoryError
from .contracts import (
    AgentTurnInput,
    AgentTurnResult,
    ModelApiConfig,
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


class _ApiConfigUpdateParams(ModelApiConfigInput):
    """Combine the target identity with a complete replacement configuration."""

    api_config_id: UUID


class AgentTurnRequest(BaseModel):
    """Validate the only secret Protocol payload accepted for an Agent turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    conversation_id: UUID | None = None
    ssh_session_id: UUID
    api_config_id: UUID
    api_key_credential_id: UUID
    api_key_b64: str = Field(repr=False)
    user_message: Annotated[str, StringConstraints(min_length=1, max_length=65536)]

    @field_validator("api_key_b64")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        """Require non-empty canonical Base64 within the explicit secret-size bound."""

        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("API key must use valid base64") from error
        if not decoded or len(decoded) > 65536:
            raise ValueError("API key length is invalid")
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("API key must use canonical base64")
        return value

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
) -> None:
    """Register configuration CRUD and the single secret Agent turn method."""

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

    async def get_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Return one non-secret configuration or a stable not-found error."""

        params = _params(raw_params, _ApiConfigIdParams)
        context.require_active()
        config = api_configs.get(params.api_config_id)
        if config is None:
            raise DispatchError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        return {"config": config.model_dump(mode="json")}

    async def create_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist one strict configuration containing only a Vault reference."""

        value = _params(raw_params, ModelApiConfigInput)
        context.require_active()
        return {"config": api_configs.create(value).model_dump(mode="json")}

    async def update_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Replace one complete non-secret configuration."""

        params = _params(raw_params, _ApiConfigUpdateParams)
        context.require_active()
        value = ModelApiConfigInput.model_validate(
            params.model_dump(exclude={"api_config_id"})
        )
        return {
            "config": api_configs.update(
                params.api_config_id,
                value,
            ).model_dump(mode="json")
        }

    async def delete_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Delete only Sidecar metadata and leave Vault secret ownership to Rust."""

        params = _params(raw_params, _ApiConfigIdParams)
        context.require_active()
        return {"deleted": api_configs.delete(params.api_config_id)}

    async def run_turn(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """Recheck secret identity, decode briefly, and return only AgentTurnResult."""

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
        if config.api_key_secret_ref != params.api_key_credential_id:
            raise DispatchError(
                "MODEL_API_CONFIG_CHANGED",
                "model API key reference changed before turn dispatch",
            )
        context.require_active()

        decoded = _decode_api_key(params.api_key_b64)
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
            _zeroize(decoded)

    handlers = {
        "agent.api_configs.list": list_configs,
        "agent.api_configs.get": get_config,
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


def _decode_api_key(value: str) -> bytearray:
    """Allocate the only mutable plaintext API key buffer used by the handler."""

    try:
        return bytearray(base64.b64decode(value, validate=True))
    except (ValueError, binascii.Error) as error:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "API key encoding is invalid",
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

    return wrapped


def _zeroize(value: bytearray) -> None:
    """Overwrite a temporary decoded secret buffer in place."""

    for index in range(len(value)):
        value[index] = 0
