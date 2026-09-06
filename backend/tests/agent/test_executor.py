from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import asyncssh
import pytest

from harness_shell_sidecar.agent.executor import AgentCancelled, SshCommandExecutor
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry


class FakeCommandProcess:
    """可控非 PTY SSH 进程，用于验证执行器所有权。"""

    def __init__(self, outcome: object, *, cleanup_failure: str | None = None) -> None:
        """保存作为进程结果的返回值、异常或等待门禁。"""

        self.outcome = outcome  # wait() 返回或抛出的值。
        self.cleanup_failure = cleanup_failure  # 注入的清理阶段失败。
        self.terminated = False  # 执行器是否发送了远程终止。
        self.closed = False  # 执行器是否关闭了本地通道。
        self.wait_closed_called = False  # 是否尝试了最终通道等待。
        self.waited_closed = False  # 本地通道清理是否完成。
        self.wait_started = asyncio.Event()  # 在完成前发出已注册信号。

    async def wait(self, *, check: bool, timeout: int) -> object:
        """记录等待选项后返回或抛出配置结果。"""

        assert check is False
        assert timeout == 30
        self.wait_started.set()
        if isinstance(self.outcome, asyncio.Event):
            gate = self.outcome
            await gate.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def close(self) -> None:
        """记录本地 SSH exec 通道关闭。"""

        self.closed = True
        if self.cleanup_failure == "close":
            raise RuntimeError("close failed")

    def terminate(self) -> None:
        """记录通道关闭前发送的显式远程终止。"""

        self.terminated = True
        if self.cleanup_failure == "terminate":
            raise RuntimeError("terminate failed")

    async def wait_closed(self) -> None:
        """记录本地通道清理完成。"""

        self.wait_closed_called = True
        if self.cleanup_failure == "wait_closed":
            raise RuntimeError("wait_closed failed")
        self.waited_closed = True


@dataclass(slots=True)
class FakeCommandConnection:
    """返回固定进程并记录精确非 PTY 派发调用。"""

    process: FakeCommandProcess
    create_gate: asyncio.Event | None = None
    calls: list[tuple[str, dict[str, object]]] = field(
        init=False,
        default_factory=list,
    )
    create_started: asyncio.Event = field(
        init=False,
        default_factory=asyncio.Event,
    )

    def __post_init__(self) -> None:
        """初始化空派发记录。"""

        self.calls.clear()

    async def create_process(
        self, command: str, **options: object
    ) -> FakeCommandProcess:
        """记录原始命令和二进制编码选项。"""

        self.calls.append((command, options))
        self.create_started.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        return self.process

    def close(self) -> None:
        """满足 SSH 注册表的连接所有权契约。"""

    async def wait_closed(self) -> None:
        """满足 SSH 注册表的连接所有权契约。"""


def _completed(
    *,
    stdout: bytes = b"out",
    stderr: bytes = b"",
    exit_status: int | None = 0,
    exit_signal: tuple[str, bool, str, str] | None = None,
) -> SimpleNamespace:
    """构建执行器使用的 AsyncSSH 完成进程数据子集。"""

    return SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        exit_status=exit_status,
        exit_signal=exit_signal,
    )


def _clock(*values: float) -> Any:
    """根据给定值返回确定性单调时钟。"""

    iterator: Iterator[float] = iter(values)
    return lambda: next(iterator)


def _registered(
    process: FakeCommandProcess,
    *,
    create_gate: asyncio.Event | None = None,
) -> tuple[SshSessionRegistry, UUID, FakeCommandConnection]:
    """注册活动 SSH 管理者替身并返回稳定会话标识。"""

    sessions = SshSessionRegistry()
    connection = FakeCommandConnection(process, create_gate=create_gate)
    session = sessions.register(
        uuid4(),
        connection,
        connection_profile_version=1,
        host_label="agent-test",
        target_host_key_fingerprint="SHA256:agent-test",
    )
    return sessions, session.ssh_session_id, connection


def test_missing_session_does_not_create_process() -> None:
    """冻结 SSH 会话不可用时在派发前失败。"""

    async def scenario() -> None:
        sessions, _session_id, connection = _registered(
            FakeCommandProcess(_completed())
        )
        executor = SshCommandExecutor(sessions, monotonic=_clock(1.0, 1.1))

        envelope = await executor.execute(uuid4(), "pwd", asyncio.Event())

        assert envelope.code == "SSH_SESSION_UNAVAILABLE"
        assert envelope.ok is False
        assert envelope.result is None
        assert connection.calls == []

    asyncio.run(scenario())


def test_pre_cancelled_turn_does_not_create_process() -> None:
    """在 SSH 派发边界前观察取消，不执行任何命令。"""

    async def scenario() -> None:
        process = FakeCommandProcess(_completed())
        sessions, session_id, connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(1.0))
        cancelled = asyncio.Event()
        cancelled.set()

        with pytest.raises(AgentCancelled):
            await executor.execute(session_id, "touch must-not-exist", cancelled)

        assert connection.calls == []
        owner = sessions.get(session_id)
        assert owner is not None
        assert owner.child_channels == set()

    asyncio.run(scenario())


def test_selected_session_owns_process_and_nonzero_exit_is_completed() -> None:
    """只使用选定管理者，并将远程非零退出视为已确定结果。"""

    async def scenario() -> None:
        waiting = asyncio.Event()
        process = FakeCommandProcess(waiting)
        sessions, session_id, connection = _registered(process)
        other_process = FakeCommandProcess(_completed(stdout=b"wrong"))
        other_connection = FakeCommandConnection(other_process)
        sessions.register(
            uuid4(),
            other_connection,
            connection_profile_version=1,
            host_label="other-agent-test",
            target_host_key_fingerprint="SHA256:other-agent-test",
        )
        executor = SshCommandExecutor(sessions, monotonic=_clock(2.0, 2.042))

        execution = asyncio.create_task(
            executor.execute(session_id, "false", asyncio.Event())
        )
        await process.wait_started.wait()
        owner = sessions.get(session_id)
        assert owner is not None
        assert process in owner.child_channels
        process.outcome = _completed(stderr=b"failed", exit_status=1)
        waiting.set()
        envelope = await execution

        assert connection.calls == [("false", {"encoding": None})]
        assert envelope.code == "COMMAND_COMPLETED"
        assert envelope.ok is True
        assert envelope.result is not None
        assert envelope.result.exit_code == 1
        assert envelope.result.stderr == "failed"
        assert envelope.result.duration_ms == 42
        assert process not in owner.child_channels
        assert other_process.wait_started.is_set() is False
        assert other_connection.calls == []

    asyncio.run(scenario())


def test_timeout_returns_partial_bytes_without_retry() -> None:
    """单次派发超时后返回严格部分输出并关闭通道。"""

    async def scenario() -> None:
        timeout = asyncssh.TimeoutError(
            None,
            "sleep 60",
            None,
            None,
            None,
            None,
            b"partial stdout",
            b"partial stderr",
        )
        process = FakeCommandProcess(timeout)
        sessions, session_id, connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(5.0, 35.0))

        envelope = await executor.execute(session_id, "sleep 60", asyncio.Event())

        assert len(connection.calls) == 1
        assert envelope.code == "COMMAND_TIMEOUT"
        assert envelope.ok is False
        assert envelope.result is not None
        assert envelope.result.stdout == "partial stdout"
        assert envelope.result.stderr == "partial stderr"
        assert envelope.result.timed_out is True
        assert process.terminated is True
        assert process.closed is True
        assert process.waited_closed is True

    asyncio.run(scenario())


def test_cancel_closes_and_unregisters_process() -> None:
    """关闭并注销通道后才抛出稳定取消错误。"""

    async def scenario() -> None:
        process = FakeCommandProcess(asyncio.Event())
        sessions, session_id, _connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(1.0, 1.0))
        cancelled = asyncio.Event()

        execution = asyncio.create_task(
            executor.execute(session_id, "sleep 60", cancelled)
        )
        await process.wait_started.wait()
        cancelled.set()
        with pytest.raises(AgentCancelled) as error:
            await execution

        owner = sessions.get(session_id)
        assert owner is not None
        assert error.value.error_code == "AGENT_CANCELLED"
        assert process.terminated is True
        assert process.closed is True
        assert process.waited_closed is True
        assert process not in owner.child_channels

    asyncio.run(scenario())


def test_invalid_utf8_returns_explicit_failure_without_replacement_text() -> None:
    """拒绝非 UTF-8 输出，不静默替换字节。"""

    async def scenario() -> None:
        process = FakeCommandProcess(_completed(stdout=b"valid\xffinvalid"))
        sessions, session_id, _connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(3.0, 3.1))

        envelope = await executor.execute(session_id, "emit", asyncio.Event())

        assert envelope.code == "COMMAND_OUTPUT_INVALID_UTF8"
        assert envelope.ok is False
        assert envelope.result is None
        assert "\ufffd" not in envelope.model_dump_json()

    asyncio.run(scenario())


def test_disconnect_after_dispatch_returns_unknown_result_without_retry() -> None:
    """只分类一次传输丢失，不重放结果不明确的命令。"""

    async def scenario() -> None:
        process = FakeCommandProcess(asyncssh.DisconnectError(10, "lost"))
        sessions, session_id, connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(3.0, 3.1))

        envelope = await executor.execute(session_id, "touch marker", asyncio.Event())

        assert len(connection.calls) == 1
        assert envelope.code == "SSH_SESSION_LOST"
        assert envelope.ok is False
        assert envelope.result is None
        owner = sessions.get(session_id)
        assert owner is not None
        assert process.terminated is True
        assert process.closed is True
        assert process.waited_closed is True
        assert process not in owner.child_channels

    asyncio.run(scenario())


def test_unexpected_wait_failure_closes_and_unregisters_process() -> None:
    """映射意外等待失败前清理拥有的通道。"""

    async def scenario() -> None:
        process = FakeCommandProcess(RuntimeError("wait failed"))
        sessions, session_id, _connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(3.0))

        envelope = await executor.execute(session_id, "pwd", asyncio.Event())

        owner = sessions.get(session_id)
        assert owner is not None
        assert envelope.code == "COMMAND_EXECUTION_ERROR"
        assert process.terminated is True
        assert process.closed is True
        assert process.waited_closed is True
        assert process not in owner.child_channels

    asyncio.run(scenario())


def test_outer_task_cancellation_closes_and_unregisters_process() -> None:
    """执行器协程取消时保留所有权直到完成清理。"""

    async def scenario() -> None:
        process = FakeCommandProcess(asyncio.Event())
        sessions, session_id, _connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(3.0))

        execution = asyncio.create_task(
            executor.execute(session_id, "sleep 60", asyncio.Event())
        )
        await process.wait_started.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

        owner = sessions.get(session_id)
        assert owner is not None
        assert process.terminated is True
        assert process.closed is True
        assert process.waited_closed is True
        assert process not in owner.child_channels

    asyncio.run(scenario())


def test_outer_cancellation_during_dispatch_waits_for_and_closes_process() -> None:
    """exec 派发先于延迟进程句柄返回时仍保留所有权。"""

    async def scenario() -> None:
        create_gate = asyncio.Event()
        process = FakeCommandProcess(asyncio.Event())
        sessions, session_id, connection = _registered(
            process,
            create_gate=create_gate,
        )
        executor = SshCommandExecutor(sessions, monotonic=_clock(3.0))

        execution = asyncio.create_task(
            executor.execute(session_id, "touch marker", asyncio.Event())
        )
        await connection.create_started.wait()
        execution.cancel()
        await asyncio.sleep(0)
        assert execution.done() is False

        create_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await execution

        owner = sessions.get(session_id)
        assert owner is not None
        assert connection.calls == [("touch marker", {"encoding": None})]
        assert process.terminated is True
        assert process.closed is True
        assert process.waited_closed is True
        assert process not in owner.child_channels

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cleanup_failure",
    ["terminate", "close", "wait_closed"],
)
def test_cleanup_failure_attempts_all_phases_and_preserves_ownership_until_closed(
    cleanup_failure: str,
) -> None:
    """尝试每个清理阶段，确认关闭前始终保留所有权。"""

    async def scenario() -> None:
        process = FakeCommandProcess(
            RuntimeError("wait failed"),
            cleanup_failure=cleanup_failure,
        )
        sessions, session_id, _connection = _registered(process)
        executor = SshCommandExecutor(sessions, monotonic=_clock(3.0))

        with pytest.raises(RuntimeError, match=f"{cleanup_failure} failed"):
            await executor.execute(session_id, "pwd", asyncio.Event())

        owner = sessions.get(session_id)
        assert owner is not None
        assert process.terminated is True
        assert process.closed is True
        assert process.wait_closed_called is True
        if cleanup_failure == "wait_closed":
            assert process in owner.child_channels
            assert process.waited_closed is False
        else:
            assert process not in owner.child_channels
            assert process.waited_closed is True

    asyncio.run(scenario())
