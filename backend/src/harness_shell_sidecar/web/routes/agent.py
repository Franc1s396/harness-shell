"""Typed HTTP routes for Provider configuration and streaming Agent turns."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import StreamingResponse

from harness_shell_sidecar.agent.contracts import ModelApiConfig
from harness_shell_sidecar.agent.handlers import (
    AgentTurnRequest,
    ModelApiConfigCreateRequest,
    ModelApiConfigUpdateRequest,
)
from harness_shell_sidecar.runtime.dispatcher import DispatchError

from ..agent_stream import AgentTurnStreamSession
from ..dependencies import (
    dispatch_application,
    dispatch_error_problem,
    model_from_result,
    require_ready_resources,
    require_request_id,
    runtime_owner,
    set_correlation,
    validate_json_model,
)
from ..errors import HttpProblem, build_problem
from ..lifespan import RuntimeOwner
from ..limits import ResponseLimitRoute
from ..models import (
    AgentApiConfigListResponse,
    AgentApiConfigResponse,
    DeleteResponse,
)


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


@router.get("/v1/agent/api-configs", response_model=AgentApiConfigListResponse)
async def list_api_configs(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentApiConfigListResponse:
    """Return all non-secret Provider configurations."""

    result = await dispatch_application(
        owner, request_id, "agent.api_configs.list", {}
    )
    configs = [
        model_from_result(value, ModelApiConfig) for value in result["configs"]
    ]
    set_correlation(response, request_id)
    return AgentApiConfigListResponse(request_id=request_id, configs=configs)


@router.post(
    "/v1/agent/api-configs",
    response_model=AgentApiConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_config(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentApiConfigResponse:
    """Persist one Provider configuration with its encrypted API key."""

    value = validate_json_model(payload, ModelApiConfigCreateRequest, request_id)
    result = await dispatch_application(
        owner,
        request_id,
        "agent.api_configs.create",
        value.model_dump(mode="json"),
    )
    set_correlation(response, request_id)
    return AgentApiConfigResponse(
        request_id=request_id,
        config=model_from_result(result["config"], ModelApiConfig),
    )


@router.patch(
    "/v1/agent/api-configs/{api_config_id}",
    response_model=AgentApiConfigResponse,
)
async def update_api_config(
    api_config_id: UUID,
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentApiConfigResponse:
    """Replace one complete non-secret Provider configuration."""

    value = validate_json_model(payload, ModelApiConfigUpdateRequest, request_id)
    params = value.model_dump(mode="json")
    params["api_config_id"] = str(api_config_id)
    result = await dispatch_application(
        owner, request_id, "agent.api_configs.update", params
    )
    set_correlation(response, request_id)
    return AgentApiConfigResponse(
        request_id=request_id,
        config=model_from_result(result["config"], ModelApiConfig),
    )


@router.delete(
    "/v1/agent/api-configs/{api_config_id}",
    response_model=DeleteResponse,
)
async def delete_api_config(
    api_config_id: UUID,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> DeleteResponse:
    """Delete Provider metadata and its owned credential atomically."""

    result = await dispatch_application(
        owner,
        request_id,
        "agent.api_configs.delete",
        {"api_config_id": str(api_config_id)},
    )
    set_correlation(response, request_id)
    return DeleteResponse(request_id=request_id, deleted=bool(result["deleted"]))


@router.post("/v1/agent/turns", response_model=None)
async def run_agent_turn(
    payload: dict[str, object],
    request_id: CorrelationId,
    owner: Owner,
    accept: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Start one Agent SSE response only after its durable Run exists."""

    if accept is None or accept.strip().lower() != "text/event-stream":
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=406,
                error_code="AGENT_STREAM_ACCEPT_REQUIRED",
                title="Agent stream accept required",
                message="Accept must be text/event-stream",
            )
        )
    value = validate_json_model(payload, AgentTurnRequest, request_id)
    resources = require_ready_resources(owner, request_id)
    session = AgentTurnStreamSession(
        request_id=request_id,
        dispatcher=resources.dispatcher,
        application=resources.agent_turn_application,
        params=value.model_dump(mode="json"),
    )
    try:
        await session.start()
    except DispatchError as error:
        raise dispatch_error_problem(request_id, error) from None
    return StreamingResponse(
        session.body(),
        status_code=200,
        media_type="text/event-stream",
        headers={
            "X-Request-ID": str(request_id),
            "Cache-Control": "no-store",
        },
    )
