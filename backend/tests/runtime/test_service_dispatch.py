from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from harness_shell_sidecar.protocol import (
    FrameEnvelope,
    MessageType,
    Sensitivity,
)
from harness_shell_sidecar.runtime import RequestDispatcher, SidecarService


class MemoryTransport:
    """记录启动/关闭状态的内存 Sidecar 传输替身。"""

    def __init__(self) -> None:
        """创建空队列及尚未启动、尚未关闭的生命周期状态。"""

        self.input: asyncio.Queue[list[FrameEnvelope] | None] = asyncio.Queue()  # 入站批次。
        self.output: asyncio.Queue[FrameEnvelope] = asyncio.Queue()  # 出站帧。
        self.started = False  # start 是否被调用。
        self.closed = False  # close 是否被调用。

    def start(self) -> None:
        """记录服务已启动传输。"""

        self.started = True

    async def read(self) -> list[FrameEnvelope] | None:
        """等待测试注入下一批帧或 EOF。"""

        return await self.input.get()

    async def send(self, frame: FrameEnvelope) -> None:
        """保存服务发出的协议帧。"""

        await self.output.put(frame)

    async def close(self) -> None:
        """记录服务已关闭传输。"""

        self.closed = True


class CleanupProbe:
    """记录 close_all 调用并可注入清理失败的测试探针。"""

    def __init__(self, *, fail: bool = False) -> None:
        """配置探针是否在记录关闭后抛出预期异常。"""

        self.closed = False  # close_all 是否被调用。
        self.fail = fail  # 是否注入清理失败。

    async def close_all(self) -> None:
        """记录清理，并按配置抛出 OSError。"""

        self.closed = True
        if self.fail:
            raise OSError("PTY cleanup failed")


class FailingCloseDispatcher(RequestDispatcher):
    """总是在 close 阶段失败的 RequestDispatcher 测试替身。"""

    def __init__(self) -> None:
        """初始化真实分发器状态和关闭调用标记。"""

        super().__init__()
        self.closed = False  # close 是否被调用。

    async def close(self) -> None:
        """记录关闭调用后注入清理失败。"""

        self.closed = True
        raise OSError("dispatcher cleanup failed")


def frame(
    sequence: int,
    payload: dict,
    *,
    message_type: MessageType = MessageType.REQUEST,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=message_type,
        request_id=uuid4(),
        task_id=None,
        workflow_run_id=None,
        sequence=sequence,
        timestamp=datetime.now(timezone.utc),
        sensitivity=sensitivity,
        payload=payload,
    )


def test_service_sends_heartbeat_before_slow_application_response(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(request: FrameEnvelope, cancelled: asyncio.Event) -> dict:
            started.set()
            await release.wait()
            return {"result": "done", "cancelled": cancelled.is_set()}

        dispatcher = RequestDispatcher()
        dispatcher.register("slow.read", slow)
        transport = MemoryTransport()
        service = SidecarService(transport, dispatcher=dispatcher)
        running = asyncio.create_task(service.run())

        ready = await transport.output.get()
        assert ready.payload["event"] == "sidecar.ready"
        initialize = frame(
            1,
            {
                "method": "initialize",
                "app_version": "0.1.0",
                "runtime_db_path": str((tmp_path / "runtime.sqlite3").resolve()),
                "runtime_data_key_b64": base64.b64encode(b"d" * 32).decode("ascii"),
                "audit_hmac_key_b64": base64.b64encode(b"a" * 32).decode("ascii"),
                "heartbeat_interval_ms": 5_000,
                "heartbeat_timeout_ms": 15_000,
            },
            sensitivity=Sensitivity.SECRET,
        )
        await transport.input.put([initialize])
        assert (await transport.output.get()).payload["result"] == "initialized"

        slow_frame = frame(2, {"method": "slow.read"})
        await transport.input.put([slow_frame])
        await started.wait()
        ping = frame(
            3,
            {"kind": "ping"},
            message_type=MessageType.HEARTBEAT,
        )
        await transport.input.put([ping])
        pong = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert pong.request_id == ping.request_id
        assert pong.payload == {"kind": "pong"}

        release.set()
        response = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert response.request_id == slow_frame.request_id
        assert response.payload == {"result": "done", "cancelled": False}

        await transport.input.put([frame(4, {"method": "shutdown"})])
        assert (await transport.output.get()).payload == {"result": "stopping"}
        assert await running == 0
        assert transport.closed is True

    asyncio.run(scenario())


def test_service_cleanup_attempts_every_stage_after_an_earlier_failure() -> None:
    async def scenario() -> None:
        transport = MemoryTransport()
        dispatcher = FailingCloseDispatcher()
        service = SidecarService(transport, dispatcher=dispatcher)
        pty = CleanupProbe(fail=True)
        ssh = CleanupProbe()
        service._pty_manager = pty
        service._ssh_runtime = ssh
        await transport.input.put(None)

        with pytest.raises(OSError, match="dispatcher cleanup failed"):
            await service.run()

        assert dispatcher.closed is True
        assert pty.closed is True
        assert ssh.closed is True
        assert transport.closed is True

    asyncio.run(scenario())
