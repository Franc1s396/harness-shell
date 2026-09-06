"""显式 SSH Host Key 信任决策的 typed HTTP 路由。"""

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
    """将新候选绑定到被替换的精确可信指纹。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: 作出替换决策前观察到的当前活动指纹。
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
    """检查直连或单层跳板 Host Key，不保留凭据。"""

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
    """持久化用户显式首次信任确认的 Host Key。"""

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
    """使用显式比较交换指纹替换可信 Host Key。"""

    value = validate_json_model(payload, HostKeyReplacementRequest, request_id)
    result = await dispatch_application(
        owner, request_id, "host_key.replace", value.model_dump(mode="json")
    )
    set_correlation(response, request_id)
    return HostKeyResponse(
        request_id=request_id,
        host_key=model_from_result(result["host_key"], HostKeyRecord),
    )
