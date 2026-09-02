"""Typed HTTP routes for Provider configuration and non-streaming Agent turns."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from harness_shell_sidecar.agent.contracts import (
    AgentTurnResult,
    ModelApiConfig,
    ModelApiConfigInput,
)
from harness_shell_sidecar.agent.handlers import AgentTurnRequest

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
from ..models import (
    AgentApiConfigListResponse,
    AgentApiConfigResponse,
    AgentTurnResponse,
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


@router.get(
    "/v1/agent/api-configs/{api_config_id}",
    response_model=AgentApiConfigResponse,
)
async def get_api_config(
    api_config_id: UUID,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentApiConfigResponse:
    """Return one non-secret Provider configuration."""

    result = await dispatch_application(
        owner,
        request_id,
        "agent.api_configs.get",
        {"api_config_id": str(api_config_id)},
    )
    set_correlation(response, request_id)
    return AgentApiConfigResponse(
        request_id=request_id,
        config=model_from_result(result["config"], ModelApiConfig),
    )


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
    """Persist one Provider configuration containing only a Vault reference."""

    value = validate_json_model(payload, ModelApiConfigInput, request_id)
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

    value = validate_json_model(payload, ModelApiConfigInput, request_id)
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
    """Delete Sidecar metadata without changing Rust Vault ownership."""

    result = await dispatch_application(
        owner,
        request_id,
        "agent.api_configs.delete",
        {"api_config_id": str(api_config_id)},
    )
    set_correlation(response, request_id)
    return DeleteResponse(request_id=request_id, deleted=bool(result["deleted"]))


@router.post("/v1/agent/turns", response_model=AgentTurnResponse)
async def run_agent_turn(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentTurnResponse:
    """Run one complete non-streaming Agent turn with transient API key bytes."""

    value = validate_json_model(payload, AgentTurnRequest, request_id)
    result = await dispatch_application(
        owner, request_id, "agent.turn.run", value.model_dump(mode="json")
    )
    turn = model_from_result(result, AgentTurnResult)
    set_correlation(response, request_id)
    return AgentTurnResponse(
        request_id=request_id,
        **turn.model_dump(),
    )
