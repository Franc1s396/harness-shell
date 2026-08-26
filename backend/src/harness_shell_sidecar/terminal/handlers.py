"""Validated Sidecar handlers for interactive PTY channels."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from harness_shell_sidecar.protocol import FrameEnvelope, Sensitivity
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher

from .manager import MAX_PTY_CHUNK_BYTES, PtyManager, PtyManagerError
from .models import PtyCols, PtyRows


class _OpenParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ssh_session_id: UUID
    cols: PtyCols
    rows: PtyRows


class _WriteParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pty_session_id: UUID
    data_b64: str


class _ResizeParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pty_session_id: UUID
    cols: PtyCols
    rows: PtyRows


class _CloseParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pty_session_id: UUID


def register_terminal_handlers(
    dispatcher: RequestDispatcher, manager: PtyManager
) -> None:
    async def open_pty(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _OpenParams)
        _require_active(cancelled)
        try:
            session = await manager.open(
                params.ssh_session_id, cols=params.cols, rows=params.rows
            )
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"pty_session": session.model_dump(mode="json")}

    async def write_pty(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        if frame.sensitivity is not Sensitivity.SECRET:
            raise DispatchError(
                "SENSITIVE_FRAME_REQUIRED", "pty.write requires a secret frame"
            )
        params = _params(frame, _WriteParams)
        data = _decode_chunk(params.data_b64)
        _require_active(cancelled)
        try:
            await manager.write(params.pty_session_id, data)
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"accepted_bytes": len(data)}

    async def resize_pty(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _ResizeParams)
        _require_active(cancelled)
        try:
            session = await manager.resize(
                params.pty_session_id, cols=params.cols, rows=params.rows
            )
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"pty_session": session.model_dump(mode="json")}

    async def close_pty(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _CloseParams)
        _require_active(cancelled)
        try:
            session = await manager.close(params.pty_session_id)
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"pty_session": session.model_dump(mode="json")}

    dispatcher.register("pty.open", open_pty)
    dispatcher.register("pty.write", write_pty)
    dispatcher.register("pty.resize", resize_pty)
    dispatcher.register("pty.close", close_pty)


def _params(frame: FrameEnvelope, model: type[BaseModel]):
    params = frame.payload.get("params")
    if not isinstance(params, dict):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        return model.model_validate_json(json.dumps(params))
    except (TypeError, ValueError, ValidationError) as exc:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params are invalid"
        ) from exc


def _decode_chunk(encoded: str) -> bytes:
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DispatchError(
            "INVALID_PTY_INPUT", "PTY input must use canonical base64"
        ) from exc
    if (
        not data
        or len(data) > MAX_PTY_CHUNK_BYTES
        or base64.b64encode(data).decode("ascii") != encoded
    ):
        raise DispatchError(
            "INVALID_PTY_INPUT", "PTY input must contain 1..32768 bytes"
        )
    return data


def _require_active(cancelled: asyncio.Event) -> None:
    if cancelled.is_set():
        raise DispatchError("REQUEST_CANCELLED", "request was cancelled")


def _dispatch_error(error: PtyManagerError) -> DispatchError:
    return DispatchError(error.error_code, "PTY operation failed")
