"""Bounded, channel-isolated remote command execution for future Agent use."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry

from .artifacts import ArtifactStore
from .models import RemoteExecRequest, RemoteExecResult


HARD_STREAM_CAP_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class RemoteExecError(RuntimeError):
    def __init__(self, error_code: str, result: RemoteExecResult) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.result = result


@dataclass(slots=True)
class _Capture:
    payload: bytearray = field(default_factory=bytearray)
    observed: int = 0
    complete: bool = False
    hard_limit_exceeded: bool = False


class RemoteExecutor:
    def __init__(
        self, ssh_sessions: SshSessionRegistry, artifacts: ArtifactStore
    ) -> None:
        self._ssh_sessions = ssh_sessions
        self._artifacts = artifacts

    async def run(self, request: RemoteExecRequest) -> RemoteExecResult:
        owner = self._ssh_sessions.get(request.ssh_session_id)
        if owner is None:
            raise RuntimeError("SSH_SESSION_NOT_FOUND")

        deadline = asyncio.get_running_loop().time() + request.timeout_ms / 1000
        try:
            async with asyncio.timeout_at(deadline):
                process = await owner.connection.create_process(
                    request.command,
                    encoding=None,
                )
        except TimeoutError as exc:
            raise RemoteExecError(
                "REMOTE_EXEC_TIMEOUT",
                self._empty_result(request, timeout=True, cancelled=False),
            ) from exc
        except asyncio.CancelledError as exc:
            raise RemoteExecError(
                "REMOTE_EXEC_CANCELLED",
                self._empty_result(request, timeout=False, cancelled=True),
            ) from exc
        owner.child_channels.add(process)
        stdout = _Capture()
        stderr = _Capture()
        tasks = (
            asyncio.create_task(self._drain(process, process.stdout, stdout)),
            asyncio.create_task(self._drain(process, process.stderr, stderr)),
        )
        timed_out = False
        cancelled = False
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.gather(*tasks)
                    await process.wait_closed()
            except TimeoutError:
                timed_out = True
                process.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await process.wait_closed()
            except asyncio.CancelledError:
                cancelled = True
                process.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await process.wait_closed()
        finally:
            owner.child_channels.discard(process)

        complete = not timed_out and not cancelled
        stdout_complete = complete and stdout.complete
        stderr_complete = complete and stderr.complete
        stdout_artifact = self._artifacts.put(
            bytes(stdout.payload),
            media_type="application/octet-stream",
            sensitivity="sensitive",
            complete=stdout_complete,
        )
        stderr_artifact = self._artifacts.put(
            bytes(stderr.payload),
            media_type="application/octet-stream",
            sensitivity="sensitive",
            complete=stderr_complete,
        )
        byte_count = stdout.observed + stderr.observed
        hard_limit = stdout.hard_limit_exceeded or stderr.hard_limit_exceeded
        result = RemoteExecResult(
            operation_id=request.operation_id,
            channel_id=uuid4(),
            exit_status=process.exit_status,
            exit_signal=process.exit_signal,
            stdout_artifact=stdout_artifact,
            stderr_artifact=stderr_artifact,
            byte_count=byte_count,
            truncated=(
                byte_count > request.output_budget_bytes
                or hard_limit
                or timed_out
                or cancelled
            ),
            timeout=timed_out,
            cancelled=cancelled,
        )
        if byte_count > request.output_budget_bytes or hard_limit:
            raise RemoteExecError("OUTPUT_LIMIT_EXCEEDED", result)
        if timed_out:
            raise RemoteExecError("REMOTE_EXEC_TIMEOUT", result)
        if cancelled:
            raise RemoteExecError("REMOTE_EXEC_CANCELLED", result)
        return result

    def _empty_result(
        self,
        request: RemoteExecRequest,
        *,
        timeout: bool,
        cancelled: bool,
    ) -> RemoteExecResult:
        stdout = self._artifacts.put(
            b"",
            media_type="application/octet-stream",
            sensitivity="sensitive",
            complete=False,
        )
        stderr = self._artifacts.put(
            b"",
            media_type="application/octet-stream",
            sensitivity="sensitive",
            complete=False,
        )
        return RemoteExecResult(
            operation_id=request.operation_id,
            channel_id=uuid4(),
            exit_status=None,
            exit_signal=None,
            stdout_artifact=stdout,
            stderr_artifact=stderr,
            byte_count=0,
            truncated=True,
            timeout=timeout,
            cancelled=cancelled,
        )

    @staticmethod
    async def _drain(process, stream, capture: _Capture) -> None:
        while True:
            chunk = await stream.read(READ_CHUNK_BYTES)
            if not chunk:
                capture.complete = True
                return
            if not isinstance(chunk, bytes):
                process.close()
                raise RuntimeError("REMOTE_STREAM_NOT_BINARY")
            capture.observed += len(chunk)
            remaining = HARD_STREAM_CAP_BYTES - len(capture.payload)
            if remaining > 0:
                capture.payload.extend(chunk[:remaining])
            if len(chunk) > remaining:
                capture.hard_limit_exceeded = True
                process.close()
                return
