"""Strict dispatcher handlers for Agent configuration and secret turn execution."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
)

from harness_shell_sidecar.protocol import (
    MAX_PAYLOAD_BYTES,
    FrameEnvelope,
    MessageType,
    Sensitivity,
)
from harness_shell_sidecar.runtime.dispatcher import (
    DispatchError,
    Handler,
    RequestDispatcher,
)

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
    api_key_b64: str
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


def register_agent_handlers(
    dispatcher: RequestDispatcher,
    api_configs: ApiConfigRepository,
    agent_service: _AgentServiceProtocol,
) -> None:
    """Register configuration CRUD and the single secret Agent turn method."""

    async def list_configs(
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
    ) -> dict:
        """Return ordered non-secret model configuration metadata."""

        _require_sensitivity(frame, Sensitivity.NORMAL)
        _params(frame, _EmptyParams)
        _require_active(cancelled)
        return {
            "configs": [config.model_dump(mode="json") for config in api_configs.list()]
        }

    async def get_config(
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
    ) -> dict:
        """Return one non-secret configuration or a stable not-found error."""

        _require_sensitivity(frame, Sensitivity.NORMAL)
        params = _params(frame, _ApiConfigIdParams)
        _require_active(cancelled)
        config = api_configs.get(params.api_config_id)
        if config is None:
            raise DispatchError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        return {"config": config.model_dump(mode="json")}

    async def create_config(
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
    ) -> dict:
        """Persist one strict configuration containing only a Vault reference."""

        _require_sensitivity(frame, Sensitivity.NORMAL)
        value = _params(frame, ModelApiConfigInput)
        _require_active(cancelled)
        return {"config": api_configs.create(value).model_dump(mode="json")}

    async def update_config(
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
    ) -> dict:
        """Replace one complete non-secret configuration."""

        _require_sensitivity(frame, Sensitivity.NORMAL)
        params = _params(frame, _ApiConfigUpdateParams)
        _require_active(cancelled)
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
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
    ) -> dict:
        """Delete only Sidecar metadata and leave Vault secret ownership to Rust."""

        _require_sensitivity(frame, Sensitivity.NORMAL)
        params = _params(frame, _ApiConfigIdParams)
        _require_active(cancelled)
        return {"deleted": api_configs.delete(params.api_config_id)}

    async def run_turn(
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
    ) -> dict:
        """Recheck secret identity, decode briefly, and return only AgentTurnResult."""

        _require_sensitivity(frame, Sensitivity.SECRET)
        params = _params(frame, AgentTurnRequest)
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
        _require_active(cancelled)

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
            request = AgentTurnInput(
                conversation_id=params.conversation_id,
                ssh_session_id=params.ssh_session_id,
                api_config_id=params.api_config_id,
                user_message=params.user_message,
            )
            result = await agent_service.run_turn(
                request,
                api_key,
                cancelled,
                expected_config=config,
            )
            payload = result.model_dump(mode="json")
            _require_agent_response_fits(frame, payload)
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


def _params(frame: FrameEnvelope, model: type[BaseModel]) -> Any:
    """Validate JSON params strictly while permitting canonical UUID JSON strings."""

    params = frame.payload.get("params")
    if not isinstance(params, dict):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "request params must be an object",
        )
    try:
        return model.model_validate_json(json.dumps(params))
    except (TypeError, ValueError, ValidationError) as error:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "request params are invalid",
        ) from error


def _require_sensitivity(
    frame: FrameEnvelope,
    expected: Sensitivity,
) -> None:
    """Enforce the exact per-method sensitivity before parsing any payload."""

    if frame.sensitivity is expected:
        return
    if expected is Sensitivity.SECRET:
        raise DispatchError(
            "AGENT_SECRET_FRAME_REQUIRED",
            "agent.turn.run requires a secret frame",
        )
    raise DispatchError(
        "AGENT_NORMAL_FRAME_REQUIRED",
        "agent.api_configs methods require a normal frame",
    )


def _decode_api_key(value: str) -> bytearray:
    """Allocate the only mutable plaintext API key buffer used by the handler."""

    try:
        return bytearray(base64.b64decode(value, validate=True))
    except (ValueError, binascii.Error) as error:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "API key encoding is invalid",
        ) from error


def _require_active(cancelled: asyncio.Event) -> None:
    """Reject a request cancelled before its next external side effect."""

    if cancelled.is_set():
        raise DispatchError("REQUEST_CANCELLED", "request was cancelled")


def _require_agent_response_fits(frame: FrameEnvelope, payload: dict) -> None:
    """Reject a result which cannot fit in one normal Protocol v1 response.

    AgentService performs the authoritative pre-COMPLETED budget check. This
    handler check is defense in depth and must never truncate, summarize, or
    move final text to an implicit fallback. A maximum u64 sequence makes the
    check conservative for every legal response emitted by the protocol peers.
    """

    candidate = FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.RESPONSE,
        request_id=frame.request_id,
        task_id=frame.task_id,
        workflow_run_id=frame.workflow_run_id,
        sequence=(2**64) - 1,
        timestamp=datetime.now(timezone.utc),
        sensitivity=Sensitivity.NORMAL,
        payload=payload,
    )
    encoded_size = len(candidate.model_dump_json(exclude_none=False).encode("utf-8"))
    if encoded_size > MAX_PAYLOAD_BYTES:
        raise DispatchError(
            "AGENT_RESPONSE_TOO_LARGE",
            "Agent response exceeds Protocol v1 payload limit",
        )


def _map_repository_errors(handler: Handler) -> Handler:
    """Convert only stable repository failures into public dispatch errors."""

    async def wrapped(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        """Invoke one handler and redact repository codes and diagnostics."""

        try:
            return await handler(frame, cancelled)
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
