from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from harness_shell_sidecar.protocol import FrameEnvelope, MessageType, Sensitivity
from harness_shell_sidecar.runtime import DispatchError, RequestDispatcher
from harness_shell_sidecar.terminal.handlers import register_terminal_handlers


class FakeManager:
    """仅记录 PTY 写入调用的最小 Manager 替身。"""

    def __init__(self) -> None:
        """创建空的 PTY 写入调用记录。"""

        self.writes: list[tuple[object, bytes]] = []  # 会话 ID 与解码后数据。

    async def write(self, pty_session_id, data: bytes) -> None:
        """记录 Handler 传递的会话 ID 与二进制正文。"""

        self.writes.append((pty_session_id, data))


def frame(params: dict, *, secret: bool) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.REQUEST,
        request_id=uuid4(),
        task_id=None,
        workflow_run_id=None,
        sequence=1,
        timestamp=datetime.now(timezone.utc),
        sensitivity=Sensitivity.SECRET if secret else Sensitivity.NORMAL,
        payload={"method": "pty.write", "params": params},
    )


def test_pty_write_requires_secret_canonical_bounded_bytes() -> None:
    async def scenario() -> None:
        manager = FakeManager()
        dispatcher = RequestDispatcher()
        register_terminal_handlers(dispatcher, manager)
        pty_session_id = uuid4()
        params = {
            "pty_session_id": str(pty_session_id),
            "data_b64": base64.b64encode("中文🙂".encode()).decode("ascii"),
        }

        with pytest.raises(DispatchError) as normal:
            await dispatcher.dispatch(frame(params, secret=False))
        assert normal.value.error_code == "SENSITIVE_FRAME_REQUIRED"

        result = await dispatcher.dispatch(frame(params, secret=True))
        assert result.payload == {"accepted_bytes": len("中文🙂".encode())}
        assert manager.writes == [(pty_session_id, "中文🙂".encode())]

        oversized = dict(params)
        oversized["data_b64"] = base64.b64encode(b"x" * 32_769).decode("ascii")
        with pytest.raises(DispatchError) as invalid:
            await dispatcher.dispatch(frame(oversized, secret=True))
        assert invalid.value.error_code == "INVALID_PTY_INPUT"

    asyncio.run(scenario())
