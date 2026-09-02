"""FastAPI dependencies for correlation and the unique runtime owner."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, TypeVar
from uuid import UUID, uuid4

from fastapi import Header, Request
from pydantic import BaseModel, ValidationError

from harness_shell_sidecar.runtime.dispatcher import DispatchError

from .errors import HttpProblem, build_problem
from .lifespan import RuntimeOwner, RuntimeOwnerError


if TYPE_CHECKING:
    from harness_shell_sidecar.runtime.resources import RuntimeResources


ModelT = TypeVar("ModelT", bound=BaseModel)


_DOMAIN_HTTP_STATUS = {
    "AGENT_CANCELLED": 409,
    "AGENT_RESPONSE_TOO_LARGE": 413,
    "AGENT_TURN_FAILED": 502,
    "CONNECTION_IN_USE": 409,
    "CONNECTION_NOT_FOUND": 404,
    "CONNECTION_PROFILE_CHANGED": 409,
    "CONNECTION_VERSION_EXHAUSTED": 409,
    "DUPLICATE_REQUEST_ID": 409,
    "HOST_KEY_ALREADY_TRUSTED": 409,
    "HOST_KEY_CHANGED": 409,
    "HOST_KEY_CONFLICT": 409,
    "HOST_KEY_ENDPOINT_MISMATCH": 409,
    "HOST_KEY_INSPECTION_FAILED": 502,
    "HOST_KEY_REPLACE_CONFLICT": 409,
    "HOST_KEY_REQUIRED": 409,
    "INVALID_PTY_INPUT": 422,
    "INVALID_REQUEST_PAYLOAD": 422,
    "INVALID_SECRET_ENCODING": 422,
    "MODEL_API_CONFIG_CHANGED": 409,
    "MODEL_API_CONFIG_DISABLED": 409,
    "MODEL_API_CONFIG_IN_USE": 409,
    "MODEL_API_CONFIG_NOT_FOUND": 404,
    "MODEL_NETWORK_TIMEOUT": 504,
    "MODEL_REQUEST_FAILED": 502,
    "MODEL_RESPONSE_INVALID": 502,
    "MULTI_HOP_PROXY_FORBIDDEN": 409,
    "PROXY_JUMP_NOT_FOUND": 404,
    "PROXY_JUMP_PROFILE_MISMATCH": 409,
    "PROXY_JUMP_SELF_REFERENCE": 409,
    "PTY_INPUT_SIZE_INVALID": 409,
    "PTY_SESSION_NOT_FOUND": 404,
    "REQUEST_CAPACITY_EXCEEDED": 429,
    "RUNTIME_STOPPING": 503,
    "SSH_AUTHENTICATION_FAILED": 502,
    "SSH_CONNECT_FAILED": 502,
    "SSH_SESSION_NOT_FOUND": 404,
    "SSH_SESSION_UNAVAILABLE": 404,
    "SFTP_ATOMIC_REPLACE_UNSUPPORTED": 501,
    "SFTP_ATTRIBUTES_INCOMPLETE": 422,
    "SFTP_ATTRIBUTES_INVALID": 422,
    "SFTP_CHANNEL_OPEN_FAILED": 502,
    "SFTP_CHUNK_INVALID": 422,
    "SFTP_CHUNK_LIMIT_EXCEEDED": 413,
    "SFTP_COMMIT_OUTCOME_UNKNOWN": 409,
    "SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED": 501,
    "SFTP_DELETE_PLAN_INVALID": 422,
    "SFTP_DELETE_PLAN_NOT_FOUND": 404,
    "SFTP_DIRECTORY_ENTRY_LIMIT_EXCEEDED": 413,
    "SFTP_DIRECTORY_NOT_EMPTY": 409,
    "SFTP_DOWNLOAD_CLEANUP_FAILED": 409,
    "SFTP_DOWNLOAD_VERIFICATION_FAILED": 409,
    "SFTP_FILE_SIZE_UNSUPPORTED": 413,
    "SFTP_FILENAME_ENCODING_UNSUPPORTED": 422,
    "SFTP_HASH_TIMEOUT": 504,
    "SFTP_LISTING_NOT_FOUND": 404,
    "SFTP_LISTING_TIMEOUT": 504,
    "SFTP_MANIFEST_TIMEOUT": 504,
    "SFTP_MUTATION_OUTCOME_UNKNOWN": 409,
    "SFTP_NOT_A_DIRECTORY": 422,
    "SFTP_NOT_A_REGULAR_FILE": 422,
    "SFTP_NOT_A_SYMLINK": 422,
    "SFTP_OPERATION_ALREADY_FINALIZED": 409,
    "SFTP_OPERATION_DUPLICATE": 409,
    "SFTP_OPERATION_NOT_FOUND": 404,
    "SFTP_OPERATION_RECORD_INVALID": 500,
    "SFTP_OPERATION_TIMEOUT": 504,
    "SFTP_OPERATION_UNSUPPORTED": 501,
    "SFTP_PATH_INVALID": 422,
    "SFTP_PERMISSION_DENIED": 403,
    "SFTP_PROTOCOL_SEQUENCE_INVALID": 409,
    "SFTP_RECOVERY_ACTION_INVALID": 422,
    "SFTP_RECOVERY_DELETE_NOT_APPLIED": 409,
    "SFTP_RECOVERY_NOT_FOUND": 404,
    "SFTP_RECOVERY_REQUIRED": 409,
    "SFTP_RECOVERY_SESSION_AMBIGUOUS": 409,
    "SFTP_RECOVERY_SESSION_MISMATCH": 409,
    "SFTP_RECOVERY_TARGET_MISSING": 409,
    "SFTP_REMOTE_READ_INVALID": 502,
    "SFTP_REMOTE_RESPONSE_INVALID": 502,
    "SFTP_REMOTE_TEMP_VERIFICATION_FAILED": 409,
    "SFTP_REQUEST_CANCELLED": 409,
    "SFTP_SESSION_NOT_CONNECTED": 404,
    "SFTP_SOURCE_HASH_MISMATCH": 409,
    "SFTP_SOURCE_NOT_FOUND": 404,
    "SFTP_SOURCE_SIZE_MISMATCH": 409,
    "SFTP_SOURCE_SNAPSHOT_INVALID": 422,
    "SFTP_TARGET_CHANGED": 409,
    "SFTP_TARGET_EXISTS": 409,
    "SFTP_TARGET_SNAPSHOT_INVALID": 422,
    "SFTP_TEMP_PATH_CONFLICT": 409,
    "SFTP_TOMBSTONE_CLEANUP_REQUIRED": 409,
    "SFTP_TOMBSTONE_MANIFEST_CHANGED": 409,
    "SFTP_TRANSFER_TIMEOUT": 504,
    "SFTP_UPLOAD_BEGIN_FAILED": 502,
    "SFTP_UPLOAD_CLEANUP_REQUIRED": 409,
    "SFTP_UPLOAD_WRITE_FAILED": 502,
}


async def require_request_id(
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> UUID:
    """Require one canonical UUID correlation header before application work."""

    raw = x_request_id
    try:
        request_id = UUID(raw) if raw is not None else None
    except (ValueError, AttributeError):
        request_id = None
    if request_id is None:
        generated = uuid4()
        request.state.request_id = generated
        raise HttpProblem(
            build_problem(
                request_id=generated,
                status=400,
                error_code="INVALID_REQUEST_ID",
                title="Invalid request ID",
                message="X-Request-ID must be a UUID",
            )
        )
    request.state.request_id = request_id
    return request_id


def runtime_owner(request: Request) -> RuntimeOwner:
    """Resolve the lifespan-owned RuntimeOwner without retaining a second graph."""

    return request.app.state.runtime_owner


def validate_json_model(
    value: Mapping[str, object],
    model: type[ModelT],
    request_id: UUID,
) -> ModelT:
    """Validate a decoded HTTP object through the model's strict JSON boundary."""

    try:
        return model.model_validate_json(
            json.dumps(dict(value), separators=(",", ":"), ensure_ascii=False)
        )
    except (TypeError, ValueError, ValidationError):
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=422,
                error_code="REQUEST_VALIDATION_FAILED",
                title="Request validation failed",
                message="Request payload is invalid",
            )
        ) from None


def model_from_result(value: object, model: type[ModelT]) -> ModelT:
    """Revalidate a dispatcher JSON value before typed HTTP serialization."""

    try:
        return model.model_validate_json(
            json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise RuntimeError("application handler returned an invalid typed result") from error


async def dispatch_application(
    owner: RuntimeOwner,
    request_id: UUID,
    operation: str,
    params: Mapping[str, object],
) -> dict[str, object]:
    """Dispatch one operation through the shared owner and map stable failures."""

    try:
        resources = owner.require_resources()
        result = await resources.dispatcher.dispatch(request_id, operation, params)
    except RuntimeOwnerError as error:
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=503,
                error_code=error.error_code,
                title="Runtime not ready",
                message=error.public_message,
            )
        ) from None
    except DispatchError as error:
        raise dispatch_error_problem(request_id, error) from None
    return result.payload


def require_ready_resources(
    owner: RuntimeOwner,
    request_id: UUID,
) -> RuntimeResources:
    """Resolve the ready graph with the same typed 503 used by dispatch routes."""

    try:
        return owner.require_resources()
    except RuntimeOwnerError as error:
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=503,
                error_code=error.error_code,
                title="Runtime not ready",
                message=error.public_message,
            )
        ) from None


def set_correlation(response, request_id: UUID) -> None:
    """Echo the validated request ID on a successful typed response."""

    response.headers["X-Request-ID"] = str(request_id)


def dispatch_error_problem(request_id: UUID, error: DispatchError) -> HttpProblem:
    """Map only stable application error codes to fixed HTTP categories."""

    code = error.error_code
    status = _DOMAIN_HTTP_STATUS.get(code)
    if status is None:
        return HttpProblem(
            build_problem(
                request_id=request_id,
                status=500,
                error_code="SIDECAR_RUNTIME_FAILED",
                title="Runtime failed",
                message="The local runtime failed",
            )
        )
    return HttpProblem(
        build_problem(
            request_id=request_id,
            status=status,
            error_code=code,
            title=code.replace("_", " ").title(),
            message=str(error),
        )
    )
