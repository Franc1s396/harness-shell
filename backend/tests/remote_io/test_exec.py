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
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    async def read(self, size: int) -> bytes:
        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else b""


class HangingReader:
    async def read(self, size: int) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeProcess:
    def __init__(self, stdout: list[bytes], stderr: list[bytes]) -> None:
        self.stdout = FakeReader(stdout)
        self.stderr = FakeReader(stderr)
        self.exit_status = 0
        self.exit_signal = None
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class HangingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__([], [])
        self.stdout = HangingReader()
        self.stderr = HangingReader()


class FakeConnection:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[dict] = []

    async def create_process(self, command: str, **options):
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
