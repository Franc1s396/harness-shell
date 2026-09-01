from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
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
from harness_shell_sidecar.telemetry import JsonLogFormatter


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


class FailingReadyTransport(MemoryTransport):
    """Fail the first outbound frame to exercise the outer runtime boundary."""

    async def send(self, frame: FrameEnvelope) -> None:
        """Raise before READY is emitted without leaking the diagnostic marker."""

        raise RuntimeError("sidecar-runtime-exception-marker")


class CleanupProbe:
    """记录 close_all 调用并可注入清理失败的测试探针。"""

    def __init__(
        self,
        *,
        fail: bool = False,
        label: str | None = None,
        order: list[str] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        """配置探针是否在记录关闭后抛出预期异常。"""

        self.closed = False  # close_all 是否被调用。
        self.fail = fail  # 是否注入清理失败。
        self.label = label  # 可选的清理顺序标识。
        self.order = order  # 多 owner 共享的关闭顺序记录。
        self.on_close = on_close  # 可选的资源顺序断言回调。

    async def close_all(self) -> None:
        """记录清理，并按配置抛出 OSError。"""

        self.closed = True
        if self.label is not None and self.order is not None:
            self.order.append(self.label)
        if self.on_close is not None:
            self.on_close()
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


def test_sidecar_service_has_no_prebuilt_agent_remote_io_fields() -> None:
    """人工 SFTP 重设计后，Sidecar 不再预构建任何 Agent Remote I/O 对象。"""

    service = SidecarService(MemoryTransport())

    assert not hasattr(service, "_artifact_store")
    assert not hasattr(service, "_remote_executor")
    assert not hasattr(service, "_remote_sftp")


def test_ready_capabilities_advertise_schema_four_and_react_shell_agent() -> None:
    """The ready frame must negotiate schema v4 and the explicit Agent feature."""

    async def scenario() -> None:
        transport = MemoryTransport()
        service = SidecarService(transport)
        running = asyncio.create_task(service.run())

        ready = await transport.output.get()
        capabilities = ready.payload["capabilities"]
        assert capabilities["storage_schema_version"] == 4
        assert "manual_sftp" in capabilities["features"]
        assert "react_shell_agent" in capabilities["features"]
        assert "agent_readonly_io" not in capabilities["features"]

        await transport.input.put(None)
        await running

    asyncio.run(scenario())


def test_runtime_initialization_registers_agent_configuration_handlers(
    tmp_path: Path,
) -> None:
    """Expose Agent handlers only after storage and SSH runtime initialization."""

    async def scenario() -> None:
        transport = MemoryTransport()
        service = SidecarService(transport)
        running = asyncio.create_task(service.run())
        await transport.output.get()
        initialize = frame(
            1,
            {
                "method": "initialize",
                "app_version": "0.1.0",
                "runtime_db_path": str((tmp_path / "agent-runtime.sqlite3").resolve()),
                "runtime_data_key_b64": base64.b64encode(b"d" * 32).decode("ascii"),
                "audit_hmac_key_b64": base64.b64encode(b"a" * 32).decode("ascii"),
                "heartbeat_interval_ms": 5_000,
                "heartbeat_timeout_ms": 15_000,
            },
            sensitivity=Sensitivity.SECRET,
        )
        await transport.input.put([initialize])
        assert (await transport.output.get()).payload["result"] == "initialized"

        request = frame(
            2,
            {"method": "agent.api_configs.list", "params": {}},
        )
        await transport.input.put([request])
        response = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert response.request_id == request.request_id
        assert response.payload == {"configs": []}

        await transport.input.put([frame(3, {"method": "shutdown"})])
        assert (await transport.output.get()).payload == {"result": "stopping"}
        assert await running == 0

    asyncio.run(scenario())


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
        order: list[str] = []
        agent_cleared: list[bool] = []
        manual_sftp = CleanupProbe(
            label="manual_sftp",
            order=order,
            on_close=lambda: agent_cleared.append(
                service._agent_service is None
                and service._agent_api_configs is None
            ),
        )
        ssh = CleanupProbe(label="ssh", order=order)
        service._agent_service = object()  # type: ignore[assignment]
        service._agent_api_configs = object()  # type: ignore[assignment]
        service._pty_manager = pty
        service._manual_sftp_service = manual_sftp
        service._ssh_runtime = ssh
        await transport.input.put(None)

        with pytest.raises(OSError, match="dispatcher cleanup failed"):
            await service.run()

        assert dispatcher.closed is True
        assert pty.closed is True
        assert manual_sftp.closed is True
        assert ssh.closed is True
        assert order == ["manual_sftp", "ssh"]
        assert agent_cleared == [True]
        assert transport.closed is True

    asyncio.run(scenario())


def test_outer_runtime_failure_logs_complete_exception_and_preserves_exit_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Map an unexpected run-loop failure after logging its complete exception."""

    async def scenario() -> None:
        caplog.set_level(logging.INFO, logger="harness_shell_sidecar.runtime")
        service = SidecarService(FailingReadyTransport())

        assert await service.run() == 1

        record = next(
            item
            for item in caplog.records
            if getattr(item, "harness_event", None) == "sidecar_runtime_failed"
        )
        assert record.harness_fields["error_code"] == "SIDECAR_RUNTIME_FAILED"
        assert "sidecar-runtime-exception-marker" in JsonLogFormatter().format(
            record
        )

    asyncio.run(scenario())


def test_request_handler_failure_logs_complete_exception_and_stable_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep the Protocol response stable while logging the complete exception."""

    async def scenario() -> None:
        async def fail_handler(
            _request: FrameEnvelope,
            _cancelled: asyncio.Event,
        ) -> dict:
            """Raise the marker which is logged but must stay out of Protocol."""

            raise RuntimeError("request-handler-exception-marker")

        caplog.set_level(logging.INFO, logger="harness_shell_sidecar.runtime")
        dispatcher = RequestDispatcher()
        dispatcher.register("failure.read", fail_handler)
        transport = MemoryTransport()
        service = SidecarService(transport, dispatcher=dispatcher)
        request = frame(1, {"method": "failure.read"})

        await service._dispatch_application_request(request)

        response = await transport.output.get()
        assert response.payload == {
            "error_code": "REQUEST_HANDLER_FAILED",
            "message": "application request handler failed",
        }
        record = next(
            item
            for item in caplog.records
            if getattr(item, "harness_event", None) == "request_handler_failed"
        )
        assert record.harness_fields["error_code"] == "REQUEST_HANDLER_FAILED"
        encoded = JsonLogFormatter().format(record)
        assert "request-handler-exception-marker" in encoded
        assert "request-handler-exception-marker" not in str(response.payload)

    asyncio.run(scenario())
