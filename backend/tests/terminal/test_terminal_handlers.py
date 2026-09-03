from __future__ import annotations

import asyncio
import base64
from uuid import uuid4

import pytest

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


def test_pty_write_accepts_only_canonical_bounded_bytes() -> None:
    async def scenario() -> None:
        manager = FakeManager()
        dispatcher = RequestDispatcher()
        register_terminal_handlers(dispatcher, manager)
        pty_session_id = uuid4()
        params = {
            "pty_session_id": str(pty_session_id),
            "data_b64": base64.b64encode("中文🙂".encode()).decode("ascii"),
        }

        result = await dispatcher.dispatch(uuid4(), "pty.write", params)
        assert result == {"accepted_bytes": len("中文🙂".encode())}
        assert manager.writes == [(pty_session_id, "中文🙂".encode())]

        invalid_base64 = dict(params)
        invalid_base64["data_b64"] = "%%%"
        with pytest.raises(DispatchError) as malformed:
            await dispatcher.dispatch(uuid4(), "pty.write", invalid_base64)
        assert malformed.value.error_code == "INVALID_PTY_INPUT"

        oversized = dict(params)
        oversized["data_b64"] = base64.b64encode(b"x" * 32_769).decode("ascii")
        with pytest.raises(DispatchError) as invalid:
            await dispatcher.dispatch(uuid4(), "pty.write", oversized)
        assert invalid.value.error_code == "INVALID_PTY_INPUT"

        unknown = dict(params)
        unknown["extra"] = True
        with pytest.raises(DispatchError) as extra:
            await dispatcher.dispatch(uuid4(), "pty.write", unknown)
        assert extra.value.error_code == "INVALID_REQUEST_PAYLOAD"

    asyncio.run(scenario())
