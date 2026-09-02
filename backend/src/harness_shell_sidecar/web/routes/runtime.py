"""Typed health and runtime lifecycle HTTP routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status

from harness_shell_sidecar.runtime import RuntimeInitializationFailure
from harness_shell_sidecar.runtime.models import (
    RuntimeInitializeRequest,
    RuntimePhase,
)

from ..dependencies import require_request_id, runtime_owner
from ..errors import HttpProblem, build_problem
from ..lifespan import RuntimeOwner, RuntimeOwnerError
from ..limits import ResponseLimitRoute
from ..models import (
    HealthLiveResponse,
    HealthReadyResponse,
    RequestCancelResponse,
    RuntimeStateResponse,
)


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


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


@router.post("/v1/runtime/initialize", response_model=RuntimeStateResponse)
async def initialize_runtime(
    payload: RuntimeInitializeRequest,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> RuntimeStateResponse:
    """Atomically initialize the unique RuntimeResources graph exactly once."""

    try:
        resources = owner.initialize_once(payload, owner.event_sink)
    except RuntimeOwnerError as error:
        raise _owner_problem(request_id, error) from None
    except RuntimeInitializationFailure as error:
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=500,
                error_code=error.error_code,
                title="Runtime initialization failed",
                message=error.public_message,
            )
        ) from None
    _set_correlation(response, request_id)
    return RuntimeStateResponse(request_id=request_id, state=resources.state)


@router.get("/v1/runtime/state", response_model=RuntimeStateResponse)
async def runtime_state(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> RuntimeStateResponse:
    """Return the current safe lifecycle state without resource details."""

    _set_correlation(response, request_id)
    return RuntimeStateResponse(request_id=request_id, state=owner.state())


@router.post(
    "/v1/runtime/shutdown",
    response_model=RuntimeStateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def shutdown_runtime(
    request: Request,
    background_tasks: BackgroundTasks,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> RuntimeStateResponse:
    """Converge the ready runtime without bypassing its owner order."""

    try:
        owner.require_resources()
    except RuntimeOwnerError as error:
        raise _owner_problem(request_id, error) from None
    state_value = await owner.shutdown()
    _set_correlation(response, request_id)
    # Run only after Starlette has flushed the typed STOPPED response. Rust can
    # then observe a normal child exit instead of racing a closed HTTP socket.
    background_tasks.add_task(request.app.state.shutdown_callback)
    return RuntimeStateResponse(request_id=request_id, state=state_value)


@router.post(
    "/v1/requests/{request_id}/cancel",
    response_model=RequestCancelResponse,
)
async def cancel_request(
    request_id: UUID,
    response: Response,
    correlation_id: CorrelationId,
    owner: Owner,
) -> RequestCancelResponse:
    """Explicitly request cooperative cancellation of one active dispatcher call."""

    try:
        resources = owner.require_resources()
    except RuntimeOwnerError as error:
        raise _owner_problem(correlation_id, error) from None
    if not await resources.dispatcher.cancel(request_id):
        raise HttpProblem(
            build_problem(
                request_id=correlation_id,
                status=404,
                error_code="REQUEST_NOT_FOUND",
                title="Request not found",
                message="The active request was not found",
            )
        )
    _set_correlation(response, correlation_id)
    return RequestCancelResponse(
        request_id=correlation_id,
        target_request_id=request_id,
        cancellation_requested=True,
    )
