"""Typed HTTP control routes for SSH sessions."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from harness_shell_sidecar.ssh.errors import ConnectionStatus
from harness_shell_sidecar.ssh.models import SshConnectRequest

from ..dependencies import (
    dispatch_application,
    model_from_result,
    require_ready_resources,
    require_request_id,
    runtime_owner,
    set_correlation,
    validate_json_model,
)
from ..lifespan import RuntimeOwner
from ..limits import ResponseLimitRoute
from ..models import SshSessionListResponse, SshStatusResponse


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


@router.get("/v1/ssh/sessions", response_model=SshSessionListResponse)
async def list_ssh_sessions(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> SshSessionListResponse:
    """Return safe metadata snapshots without AsyncSSH transport objects."""

    resources = require_ready_resources(owner, request_id)
    set_correlation(response, request_id)
    return SshSessionListResponse(
        request_id=request_id,
        sessions=list(resources.ssh_runtime.sessions.snapshots()),
    )


@router.post(
    "/v1/ssh/sessions",
    response_model=SshStatusResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_ssh_session(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> SshStatusResponse:
    """Use transient credentials to establish one verified SSH session."""

    value = validate_json_model(payload, SshConnectRequest, request_id)
    result = await dispatch_application(
        owner, request_id, "ssh.connect", value.model_dump(mode="json")
    )
    set_correlation(response, request_id)
    return SshStatusResponse(
        request_id=request_id,
        status=model_from_result(result["status"], ConnectionStatus),
    )


@router.delete(
    "/v1/ssh/sessions/{ssh_session_id}",
    response_model=SshStatusResponse,
)
async def disconnect_ssh_session(
    ssh_session_id: UUID,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> SshStatusResponse:
    """Close one SSH session and all of its child channels."""

    result = await dispatch_application(
        owner,
        request_id,
        "ssh.disconnect",
        {"ssh_session_id": str(ssh_session_id)},
    )
    set_correlation(response, request_id)
    return SshStatusResponse(
        request_id=request_id,
        status=model_from_result(result["status"], ConnectionStatus),
    )
