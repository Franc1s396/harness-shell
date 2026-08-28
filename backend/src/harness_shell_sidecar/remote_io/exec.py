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
    """携带稳定错误码和已捕获部分结果的远端执行异常。"""

    def __init__(self, error_code: str, result: RemoteExecResult) -> None:
        """保存失败类别以及可供审计的结构化执行结果。"""

        super().__init__(error_code)
        self.error_code = error_code  # 面向调用方的稳定失败类别。
        self.result = result  # 包含失败前已捕获输出的结构化结果。


@dataclass(slots=True)
class _Capture:
    """跟踪单个远端输出流的有限内存捕获状态。"""

    #: 在进程硬上限内实际保留的输出字节。
    payload: bytearray = field(default_factory=bytearray)
    #: 从流中观测到的总字节数，包括未保留的截断部分。
    observed: int = 0
    #: 是否正常读到流末尾。
    complete: bool = False
    #: 是否因超过进程级硬上限而主动关闭进程。
    hard_limit_exceeded: bool = False


class RemoteExecutor:
    """在独立 SSH channel 中执行有界命令并加密保存输出。"""

    def __init__(
        self, ssh_sessions: SshSessionRegistry, artifacts: ArtifactStore
    ) -> None:
        """绑定 SSH 会话注册表和输出 Artifact 仓储。"""

        self._ssh_sessions = ssh_sessions  # 查找并跟踪命令所属的 SSH 会话。
        self._artifacts = artifacts  # 持久化 stdout/stderr 的加密内容。

    async def run(self, request: RemoteExecRequest) -> RemoteExecResult:
        """在统一 deadline 内执行命令、排空输出并返回有界结果。"""

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
        """为命令尚未成功启动的超时或取消构造空的不完整结果。"""

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
        """持续读取一个二进制流，达到硬上限时关闭远端进程。"""

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
