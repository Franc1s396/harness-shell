from __future__ import annotations

import asyncio
import base64
from uuid import uuid4

import pytest

from harness_shell_sidecar.remote_io import RemoteExecRequest, RemoteExecutor, RemoteSftp
from harness_shell_sidecar.remote_io.exec import RemoteExecError
from harness_shell_sidecar.terminal import PtyManager


async def wait_until(predicate, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def test_interactive_bash_utf8_resize_close_and_agent_channel_isolation(
    runtime_context, connect_proxy, lab
) -> None:
    async def scenario() -> None:
        _, _, status = await connect_proxy()
        events: list[dict] = []

        async def emit(event: dict) -> None:
            events.append(event)

        terminal = PtyManager(runtime_context.runtime.sessions, event_listener=emit)
        executor = RemoteExecutor(runtime_context.runtime.sessions, runtime_context.artifacts)
        sftp = RemoteSftp(runtime_context.runtime.sessions, runtime_context.artifacts)
        pty = await terminal.open(status.session_id, cols=80, rows=24)
        exec_task = asyncio.create_task(
            executor.run(
                RemoteExecRequest(
                    operation_id=uuid4(),
                    ssh_session_id=status.session_id,
                    command="printf agent-stdout; printf agent-stderr >&2",
                    timeout_ms=60_000,
                    output_budget_bytes=1024,
                )
            )
        )
        sftp_task = asyncio.create_task(
            sftp.read_range(
                status.session_id,
                f"/home/{lab.target_username}/data.txt",
                offset=0,
                length=128,
            )
        )
        await terminal.resize(pty.pty_session_id, cols=100, rows=31)
        nonce = "pty-only-中文-🙂"
        await terminal.write(
            pty.pty_session_id,
            f"printf '{nonce}'; stty size\r".encode(),
        )
        def current_transcript() -> bytes:
            return b"".join(
                base64.b64decode(event["data_b64"])
                for event in events
                if event["event"] == "ssh.pty.output"
            )

        await wait_until(lambda: b"31 100" in current_transcript())
        transcript = current_transcript()
        await terminal.close(pty.pty_session_id)
        exec_result, read_result = await asyncio.gather(exec_task, sftp_task)
        assert nonce.encode() in transcript
        assert b"31 100" in transcript
        assert b"agent-stdout" not in transcript
        assert runtime_context.artifacts.get(exec_result.stdout_artifact.artifact_id) == b"agent-stdout"
        assert runtime_context.artifacts.get(exec_result.stderr_artifact.artifact_id) == b"agent-stderr"
        assert b"harness-shell-target-utf8" in runtime_context.artifacts.get(read_result.artifact.artifact_id)
        await runtime_context.runtime.disconnect(status.session_id)
        assert len(runtime_context.runtime.sessions) == 0

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime_context.close())


def test_exec_timeout_and_output_overflow_are_typed_artifacts(
    runtime_context, connect_proxy
) -> None:
    async def scenario() -> None:
        _, _, status = await connect_proxy()
        executor = RemoteExecutor(runtime_context.runtime.sessions, runtime_context.artifacts)
        with pytest.raises(RemoteExecError) as timeout:
            await executor.run(
                RemoteExecRequest(
                    operation_id=uuid4(),
                    ssh_session_id=status.session_id,
                    command="sleep 2",
                    timeout_ms=50,
                    output_budget_bytes=1024,
                )
            )
        assert timeout.value.error_code == "REMOTE_EXEC_TIMEOUT"
        assert timeout.value.result.timeout is True
        assert timeout.value.result.stdout_artifact.complete is False

        with pytest.raises(RemoteExecError) as overflow:
            await executor.run(
                RemoteExecRequest(
                    operation_id=uuid4(),
                    ssh_session_id=status.session_id,
                    command="head -c 2048 /dev/zero",
                    timeout_ms=60_000,
                    output_budget_bytes=1024,
                )
            )
        assert overflow.value.error_code == "OUTPUT_LIMIT_EXCEEDED"
        assert overflow.value.result.byte_count == 2048
        assert overflow.value.result.stdout_artifact.complete is True
        assert len(runtime_context.artifacts.get(overflow.value.result.stdout_artifact.artifact_id)) == 2048
        await runtime_context.runtime.disconnect(status.session_id)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime_context.close())
