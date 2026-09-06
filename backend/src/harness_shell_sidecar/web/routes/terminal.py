"""交互式 PTY 会话生命周期的 typed HTTP 控制路由。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from harness_shell_sidecar.terminal.models import PtyCols, PtyRows, PtySession

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
from ..models import PtySessionResponse


class PtyOpenRequest(BaseModel):
    """将新 PTY 绑定到活动 SSH 会话和初始尺寸。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: 将拥有 PTY 通道的活动 SSH 会话。
    ssh_session_id: UUID
    #: 初始终端列数。
    cols: PtyCols
    #: 初始终端行数。
    rows: PtyRows


class PtyResizeRequest(BaseModel):
    """描述有界 PTY 尺寸更新。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: 更新后的终端列数。
    cols: PtyCols
    #: 更新后的终端行数。
    rows: PtyRows


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


@router.post(
    "/v1/pty/sessions",
    response_model=PtySessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_pty_session(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> PtySessionResponse:
    """在显式选择的 SSH 会话上打开 PTY 通道。"""

    value = validate_json_model(payload, PtyOpenRequest, request_id)
    result = await dispatch_application(
        owner, request_id, "pty.open", value.model_dump(mode="json")
    )
    set_correlation(response, request_id)
    return PtySessionResponse(
        request_id=request_id,
        pty_session=model_from_result(result["pty_session"], PtySession),
    )


@router.post(
    "/v1/pty/sessions/{pty_session_id}/resize",
    response_model=PtySessionResponse,
)
async def resize_pty_session(
    pty_session_id: UUID,
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> PtySessionResponse:
    """调整活动 PTY 大小，不通过 HTTP 暴露字节流。"""

    value = validate_json_model(payload, PtyResizeRequest, request_id)
    params = value.model_dump(mode="json")
    params["pty_session_id"] = str(pty_session_id)
    result = await dispatch_application(owner, request_id, "pty.resize", params)
    set_correlation(response, request_id)
    return PtySessionResponse(
        request_id=request_id,
        pty_session=model_from_result(result["pty_session"], PtySession),
    )


@router.delete(
    "/v1/pty/sessions/{pty_session_id}",
    response_model=PtySessionResponse,
)
async def close_pty_session(
    pty_session_id: UUID,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> PtySessionResponse:
    """关闭活动 PTY 并返回终态快照。"""

    result = await dispatch_application(
        owner,
        request_id,
        "pty.close",
        {"pty_session_id": str(pty_session_id)},
    )
    set_correlation(response, request_id)
    return PtySessionResponse(
        request_id=request_id,
        pty_session=model_from_result(result["pty_session"], PtySession),
    )
