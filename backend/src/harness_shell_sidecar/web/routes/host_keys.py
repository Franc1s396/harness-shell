"""Typed HTTP routes for explicit SSH Host Key trust decisions."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import ConfigDict

from harness_shell_sidecar.connections.models import HostKeyCandidate, HostKeyRecord
from harness_shell_sidecar.ssh.errors import ConnectionStatus
from harness_shell_sidecar.ssh.models import HostKeyInspectionRequest

from ..dependencies import (
    dispatch_application,
    model_from_result,
    require_request_id,
    runtime_owner,
    set_correlation,
    validate_json_model,
)
from ..lifespan import RuntimeOwner
from ..limits import ResponseLimitRoute
from ..models import HostKeyResponse, SshStatusResponse


class HostKeyReplacementRequest(HostKeyCandidate):
    """Bind a new candidate to the exact trusted fingerprint being replaced."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Current active fingerprint observed before the replacement decision.
    expected_old_fingerprint: str


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


@router.post("/v1/host-key-inspections", response_model=SshStatusResponse)
async def inspect_host_key(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> SshStatusResponse:
    """Inspect a direct or single-hop Host Key without retaining credentials."""

    value = validate_json_model(payload, HostKeyInspectionRequest, request_id)
    result = await dispatch_application(
        owner, request_id, "host_key.inspect", value.model_dump(mode="json")
    )
    set_correlation(response, request_id)
    return SshStatusResponse(
        request_id=request_id,
        status=model_from_result(result["status"], ConnectionStatus),
    )


@router.post(
    "/v1/host-key-confirmations",
    response_model=HostKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_host_key(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> HostKeyResponse:
    """Persist a user's explicit first-trust Host Key confirmation."""

    value = validate_json_model(payload, HostKeyCandidate, request_id)
    result = await dispatch_application(
        owner, request_id, "host_key.confirm", value.model_dump(mode="json")
    )
    set_correlation(response, request_id)
    return HostKeyResponse(
        request_id=request_id,
        host_key=model_from_result(result["host_key"], HostKeyRecord),
    )


@router.post("/v1/host-key-replacements", response_model=HostKeyResponse)
async def replace_host_key(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> HostKeyResponse:
    """Replace a trusted Host Key with an explicit compare-and-swap fingerprint."""

    value = validate_json_model(payload, HostKeyReplacementRequest, request_id)
    result = await dispatch_application(
        owner, request_id, "host_key.replace", value.model_dump(mode="json")
    )
    set_correlation(response, request_id)
    return HostKeyResponse(
        request_id=request_id,
        host_key=model_from_result(result["host_key"], HostKeyRecord),
    )
