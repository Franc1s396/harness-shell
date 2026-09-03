"""Typed health and runtime lifecycle HTTP routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from harness_shell_sidecar.runtime.models import RuntimePhase

from ..dependencies import require_request_id, runtime_owner
from ..errors import HttpProblem, build_problem
from ..lifespan import RuntimeOwner, RuntimeOwnerError
from ..limits import ResponseLimitRoute
from ..models import (
    CredentialPublicKeyResponse,
    HealthLiveResponse,
    HealthReadyResponse,
    RuntimeStateResponse,
)


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


@router.get(
    "/v1/runtime/credential-encryption-key",
    response_model=CredentialPublicKeyResponse,
)
async def credential_encryption_key(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> CredentialPublicKeyResponse:
    """Expose only the current process-ephemeral credential public key."""

    try:
        resources = owner.require_resources()
    except RuntimeOwnerError as error:
        raise _owner_problem(request_id, error) from None
    cipher = resources.credential_cipher
    if cipher is None:
        raise RuntimeError("autonomous credential cipher is unavailable")
    public_key = cipher.public_key()
    response.headers["Cache-Control"] = "no-store"
    _set_correlation(response, request_id)
    return CredentialPublicKeyResponse(
        request_id=request_id,
        **public_key.model_dump(),
    )


def _set_correlation(response: Response, request_id: UUID) -> None:
    """Echo the validated request ID in every successful HTTP response."""

    response.headers["X-Request-ID"] = str(request_id)


def _owner_problem(request_id: UUID, error: RuntimeOwnerError) -> HttpProblem:
    """Map one stable lifecycle conflict to its fixed HTTP category."""

    status_code = 409 if error.error_code == "RUNTIME_ALREADY_INITIALIZED" else 503
    return HttpProblem(
        build_problem(
            request_id=request_id,
            status=status_code,
            error_code=error.error_code,
            title=(
                "Runtime already initialized"
                if status_code == 409
                else "Runtime not ready"
            ),
            message=error.public_message,
        )
    )


@router.get("/v1/health/live", response_model=HealthLiveResponse)
async def health_live(
    response: Response,
    request_id: CorrelationId,
) -> HealthLiveResponse:
    """Report liveness without opening or inspecting runtime persistence."""

    _set_correlation(response, request_id)
    return HealthLiveResponse(request_id=request_id, live=True)


@router.get("/v1/health/ready", response_model=HealthReadyResponse)
async def health_ready(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> HealthReadyResponse:
    """Return ready only after the complete verified graph is published."""

    state_value = owner.state()
    if state_value is not RuntimePhase.READY:
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=503,
                error_code="RUNTIME_NOT_READY",
                title="Runtime not ready",
                message="Runtime is not ready",
                details={"state": state_value.value},
            )
        )
    _set_correlation(response, request_id)
    return HealthReadyResponse(
        request_id=request_id,
        ready=True,
        state=state_value,
    )


@router.get("/v1/runtime/state", response_model=RuntimeStateResponse)
async def runtime_state(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> RuntimeStateResponse:
    """Return the current safe lifecycle state without resource details."""

    _set_correlation(response, request_id)
    return RuntimeStateResponse(request_id=request_id, state=owner.state())
