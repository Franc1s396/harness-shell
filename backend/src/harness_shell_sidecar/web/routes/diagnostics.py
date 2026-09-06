"""Python 诊断目录可用性与固定目录打开路由。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from ..dependencies import require_request_id, set_correlation
from ..errors import HttpProblem, build_problem
from ..limits import ResponseLimitRoute
from ..models import DiagnosticsAvailabilityResponse


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]


def open_log_directory_with_explorer(directory: Path) -> None:
    """为已校验的 Python 拥有目录启动 Explorer。"""

    subprocess.Popen(["explorer.exe", str(directory)])


def _log_directory(request: Request) -> Path | None:
    """仅解析应用工厂保存的不可变 Runtime 配置路径。"""

    settings = request.app.state.settings
    return None if settings is None else settings.log_dir


@router.get(
    "/v1/diagnostics/log-directory",
    response_model=DiagnosticsAvailabilityResponse,
)
async def get_log_directory_availability(
    request: Request,
    response: Response,
    request_id: CorrelationId,
) -> DiagnosticsAvailabilityResponse:
    """返回可用性，不序列化本地绝对路径。"""

    directory = _log_directory(request)
    set_correlation(response, request_id)
    return DiagnosticsAvailabilityResponse(
        request_id=request_id,
        available=directory is not None and directory.is_dir(),
    )


@router.post(
    "/v1/diagnostics/log-directory/open",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def open_log_directory(
    request: Request,
    response: Response,
    request_id: CorrelationId,
) -> None:
    """只通过 Explorer 打开由配置派生的已有目录。"""

    directory = _log_directory(request)
    if directory is None or not directory.is_dir():
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=404,
                error_code="LOG_DIRECTORY_UNAVAILABLE",
                title="Log directory unavailable",
                message="The application log directory is not available",
            )
        )
    opener = request.app.state.log_directory_opener
    try:
        opener(directory)
    except OSError:
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=500,
                error_code="LOG_DIRECTORY_OPEN_FAILED",
                title="Log directory open failed",
                message="The application log directory could not be opened",
            )
        ) from None
    set_correlation(response, request_id)
