"""Typed HTTP control routes for interactive PTY session lifecycle."""

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
    """Bind a new PTY to one active SSH session and initial geometry."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Active SSH session that will own the PTY channel.
    ssh_session_id: UUID
    #: Initial terminal columns.
    cols: PtyCols
    #: Initial terminal rows.
    rows: PtyRows


class PtyResizeRequest(BaseModel):
    """Describe a bounded PTY geometry update."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Updated terminal columns.
    cols: PtyCols
    #: Updated terminal rows.
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
    """Open one PTY channel on an explicitly selected SSH session."""

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
    """Resize one active PTY without exposing its byte stream over HTTP."""

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
    """Close one active PTY and return its terminal snapshot."""

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
