"""Python-owned diagnostics availability and fixed-directory launch routes."""

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
    """Start Explorer for the already-validated Python-owned directory."""

    subprocess.Popen(["explorer.exe", str(directory)])


def _log_directory(request: Request) -> Path | None:
    """Resolve only the immutable Runtime settings path stored by the app factory."""

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
    """Return availability without serializing the local absolute path."""

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
    """Open only the existing settings-derived directory through Explorer."""

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
