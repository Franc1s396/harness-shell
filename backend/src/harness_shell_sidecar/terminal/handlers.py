"""Validated Sidecar handlers for interactive PTY channels."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext

from .manager import MAX_PTY_CHUNK_BYTES, PtyManager, PtyManagerError
from .models import PtyCols, PtyRows


class _OpenParams(BaseModel):
    """在已有 SSH 会话上打开 PTY 的请求参数。"""

    #: 对 IPC 参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 承载新 PTY channel 的活动 SSH 会话标识符。
    ssh_session_id: UUID
    #: 初始终端列数。
    cols: PtyCols
    #: 初始终端行数。
    rows: PtyRows


class _WriteParams(BaseModel):
    """向活动 PTY 写入一段二进制输入的请求参数。"""

    #: 对 IPC 参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 接收输入的 PTY 会话标识符。
    pty_session_id: UUID
    #: 1..32768 字节输入的标准 Base64 编码。
    data_b64: str


class _ResizeParams(BaseModel):
    """调整活动 PTY 终端尺寸的请求参数。"""

    #: 对 IPC 参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 要调整尺寸的 PTY 会话标识符。
    pty_session_id: UUID
    #: 更新后的终端列数。
    cols: PtyCols
    #: 更新后的终端行数。
    rows: PtyRows


class _CloseParams(BaseModel):
    """关闭单个 PTY 会话的请求参数。"""

    #: 对 IPC 参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 要关闭的 PTY 会话标识符。
    pty_session_id: UUID


def register_terminal_handlers(
    dispatcher: RequestDispatcher, manager: PtyManager
) -> None:
    async def open_pty(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _OpenParams)
        context.require_active()
        try:
            session = await manager.open(
                params.ssh_session_id, cols=params.cols, rows=params.rows
            )
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"pty_session": session.model_dump(mode="json")}

    async def write_pty(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _WriteParams)
        data = _decode_chunk(params.data_b64)
        context.require_active()
        try:
            await manager.write(params.pty_session_id, data)
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"accepted_bytes": len(data)}

    async def resize_pty(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _ResizeParams)
        context.require_active()
        try:
            session = await manager.resize(
                params.pty_session_id, cols=params.cols, rows=params.rows
            )
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"pty_session": session.model_dump(mode="json")}

    async def close_pty(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _CloseParams)
        context.require_active()
        try:
            session = await manager.close(params.pty_session_id)
        except PtyManagerError as exc:
            raise _dispatch_error(exc) from exc
        return {"pty_session": session.model_dump(mode="json")}

    dispatcher.register("pty.open", open_pty)
    dispatcher.register("pty.write", write_pty)
    dispatcher.register("pty.resize", resize_pty)
    dispatcher.register("pty.close", close_pty)


def _params(raw_params: Mapping[str, object], model: type[BaseModel]):
    if not isinstance(raw_params, Mapping):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        return model.model_validate_json(json.dumps(dict(raw_params)))
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


def _dispatch_error(error: PtyManagerError) -> DispatchError:
    return DispatchError(error.error_code, "PTY operation failed")
