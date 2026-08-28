from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from harness_shell_sidecar.remote_io import ArtifactStore, RemoteExecRequest
from harness_shell_sidecar.remote_io.exec import (
    HARD_STREAM_CAP_BYTES,
    RemoteExecError,
    RemoteExecutor,
)
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import EncryptedRecordStore, RuntimeDatabase


class FakeReader:
    """按预设顺序返回二进制 chunk 的异步流替身。"""

    def __init__(self, chunks: list[bytes]) -> None:
        """复制 chunk 列表，避免测试读取修改调用方数据。"""

        self.chunks = list(chunks)  # 尚未被 read 消费的 chunk。

    async def read(self, size: int) -> bytes:
        """返回下一个 chunk，耗尽后模拟 EOF。"""

        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else b""


class HangingReader:
    """永不自行完成读取、用于触发执行超时的流替身。"""

    async def read(self, size: int) -> bytes:
        """永久等待，除非外部任务取消。"""

        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeProcess:
    """暴露独立 stdout/stderr 和可观察关闭状态的进程替身。"""

    def __init__(self, stdout: list[bytes], stderr: list[bytes]) -> None:
        """使用预设输出创建一个默认正常退出的进程。"""

        self.stdout = FakeReader(stdout)  # 标准输出流替身。
        self.stderr = FakeReader(stderr)  # 标准错误流替身。
        self.exit_status = 0  # 模拟正常退出码。
        self.exit_signal = None  # 正常退出时没有终止信号。
        self.closed = False  # close 是否被调用。
        self.waited = False  # wait_closed 是否被等待。

    def close(self) -> None:
        """记录执行器主动关闭了进程。"""

        self.closed = True

    async def wait_closed(self) -> None:
        """记录执行器等待了进程资源收敛。"""

        self.waited = True


class HangingProcess(FakeProcess):
    """stdout 与 stderr 均永久阻塞的超时场景进程。"""

    def __init__(self) -> None:
        """以不会自然到达 EOF 的读取器替换两个输出流。"""

        super().__init__([], [])
        self.stdout = HangingReader()  # 永不自然结束的标准输出。
        self.stderr = HangingReader()  # 永不自然结束的标准错误。


class FakeConnection:
    """返回固定进程并记录 create_process 参数的 SSH 连接替身。"""

    def __init__(self, process: FakeProcess) -> None:
        """保存唯一待返回进程并初始化调用记录。"""

        self.process = process  # 每次 create_process 返回的进程。
        self.calls: list[dict] = []  # create_process 的命令和选项记录。

    async def create_process(self, command: str, **options):
        """记录二进制 channel 选项并返回固定进程。"""

        self.calls.append({"command": command, **options})
        return self.process


def setup_executor(tmp_path: Path, process: FakeProcess):
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    records = EncryptedRecordStore(database, b"e" * 32)
    artifacts = ArtifactStore(database, records)
    sessions = SshSessionRegistry()
    connection = FakeConnection(process)
    session = sessions.register(uuid4(), connection)
    return (
        database,
        records,
        artifacts,
        session,
        connection,
        RemoteExecutor(sessions, artifacts),
    )


def request(session_id, *, budget: int = 1024) -> RemoteExecRequest:
    return RemoteExecRequest(
        operation_id=uuid4(),
        ssh_session_id=session_id,
        command="printf out; printf err >&2",
        timeout_ms=60_000,
        output_budget_bytes=budget,
    )


def test_exec_uses_distinct_binary_channel_and_encrypted_artifacts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        process = FakeProcess([b"out"], [b"err"])
        database, records, artifacts, session, connection, executor = setup_executor(
            tmp_path, process
        )
        try:
            result = await executor.run(request(session.ssh_session_id))
            assert connection.calls == [
                {
                    "command": "printf out; printf err >&2",
                    "encoding": None,
                }
            ]
            assert artifacts.get(result.stdout_artifact.artifact_id) == b"out"
            assert artifacts.get(result.stderr_artifact.artifact_id) == b"err"
            assert result.byte_count == 6
            assert result.truncated is False
            assert len(session.child_channels) == 0
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())


def test_timeout_returns_typed_partial_artifacts(tmp_path: Path) -> None:
    async def scenario() -> None:
        process = HangingProcess()
        database, records, artifacts, session, _, executor = setup_executor(
            tmp_path, process
        )
        timed = request(session.ssh_session_id).model_copy(update={"timeout_ms": 1})
        try:
            with pytest.raises(RemoteExecError) as raised:
                await executor.run(timed)
            assert raised.value.error_code == "REMOTE_EXEC_TIMEOUT"
            assert raised.value.result.timeout is True
            assert raised.value.result.truncated is True
            assert raised.value.result.stdout_artifact.complete is False
            assert artifacts.get(raised.value.result.stdout_artifact.artifact_id) == b""
            assert process.closed is True
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())


def test_output_budget_exceeded_is_typed_and_artifacts_remain_complete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        process = FakeProcess([b"o" * 800], [b"e" * 800])
        database, records, artifacts, session, _, executor = setup_executor(
            tmp_path, process
        )
        try:
            with pytest.raises(RemoteExecError) as raised:
                await executor.run(request(session.ssh_session_id, budget=1024))
            assert raised.value.error_code == "OUTPUT_LIMIT_EXCEEDED"
            result = raised.value.result
            assert result.truncated is True
            assert result.byte_count == 1600
            assert result.stdout_artifact.complete is True
            assert result.stderr_artifact.complete is True
            assert len(artifacts.get(result.stdout_artifact.artifact_id)) == 800
            assert len(artifacts.get(result.stderr_artifact.artifact_id)) == 800
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())


def test_hard_stream_cap_closes_channel_and_marks_artifact_incomplete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        observed = HARD_STREAM_CAP_BYTES + 1
        process = FakeProcess([b"x" * observed], [])
        database, records, artifacts, session, _, executor = setup_executor(
            tmp_path, process
        )
        try:
            with pytest.raises(RemoteExecError) as raised:
                await executor.run(request(session.ssh_session_id))
            assert raised.value.error_code == "OUTPUT_LIMIT_EXCEEDED"
            result = raised.value.result
            assert result.byte_count == observed
            assert result.truncated is True
            assert result.stdout_artifact.complete is False
            assert len(artifacts.get(result.stdout_artifact.artifact_id)) == HARD_STREAM_CAP_BYTES
            assert process.closed is True
            assert process.waited is True
            assert len(session.child_channels) == 0
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())
