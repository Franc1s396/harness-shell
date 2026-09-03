"""Typed HTTP routes for persisted SSH connection profiles."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from harness_shell_sidecar.connections.handlers import (
    ConnectionCreateRequest,
    ConnectionUpdateRequest,
)
from harness_shell_sidecar.connections.models import (
    ConnectionProfile,
)

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
from ..models import ConnectionListResponse, ConnectionResponse, DeleteResponse


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


@router.get("/v1/connections", response_model=ConnectionListResponse)
async def list_connections(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> ConnectionListResponse:
    """Return all persisted profiles through the shared application handler."""

    result = await dispatch_application(owner, request_id, "connections.list", {})
    connections = [
        model_from_result(value, ConnectionProfile)
        for value in result["connections"]
    ]
    set_correlation(response, request_id)
    return ConnectionListResponse(request_id=request_id, connections=connections)


@router.post(
    "/v1/connections",
    response_model=ConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> ConnectionResponse:
    """Validate and persist one complete connection profile."""

    value = validate_json_model(payload, ConnectionCreateRequest, request_id)
    result = await dispatch_application(
        owner,
        request_id,
        "connections.create",
        value.model_dump(mode="json"),
    )
    set_correlation(response, request_id)
    return ConnectionResponse(
        request_id=request_id,
        connection=model_from_result(result["connection"], ConnectionProfile),
    )


@router.patch(
    "/v1/connections/{connection_id}",
    response_model=ConnectionResponse,
)
async def update_connection(
    connection_id: UUID,
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> ConnectionResponse:
    """Replace one complete connection profile while preserving its identity."""

    value = validate_json_model(payload, ConnectionUpdateRequest, request_id)
    params = value.model_dump(mode="json")
    params["connection_id"] = str(connection_id)
    result = await dispatch_application(
        owner, request_id, "connections.update", params
    )
    set_correlation(response, request_id)
    return ConnectionResponse(
        request_id=request_id,
        connection=model_from_result(result["connection"], ConnectionProfile),
    )


@router.delete(
    "/v1/connections/{connection_id}",
    response_model=DeleteResponse,
)
async def delete_connection(
    connection_id: UUID,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> DeleteResponse:
    """Delete one profile only when its domain ownership gates allow it."""

    result = await dispatch_application(
        owner,
        request_id,
        "connections.delete",
        {"connection_id": str(connection_id)},
    )
    set_correlation(response, request_id)
    return DeleteResponse(request_id=request_id, deleted=bool(result["deleted"]))
