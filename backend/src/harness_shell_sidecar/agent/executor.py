"""在冻结的活动 SSH 会话上执行一条已审查命令。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import asyncssh

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry

from .context_models import AgentContextPolicy
from .tools import clip_output
from .contracts import CommandExecutionResult, CommandToolEnvelope


COMMAND_TIMEOUT_SECONDS = 30


class AgentCancelled(RuntimeError):
    """清理活动本地通道后中断图执行。"""

    def __init__(
        self,
        error_code: str = "AGENT_CANCELLED",
        message: str = "the Agent operation was cancelled",
    ) -> None:
        """保存稳定取消错误码及经过审查的生命周期原因。"""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # 稳定的 Run 级取消错误码。
        self.safe_message = message  # 不含命令内容的取消发生位置。


class SshCommandExecutor:
    """通过现有会话注册表管理非 PTY 的 AsyncSSH exec 通道。"""

    def __init__(
        self,
        ssh_sessions: SshSessionRegistry,
        *,
        policy: AgentContextPolicy = AgentContextPolicy(),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """绑定权威活动会话注册表和耗时时钟。"""

        self._policy = policy  # 不可变的各输出流保留配置。
        self._ssh_sessions = ssh_sessions  # 只解析本轮冻结的会话。
        self._monotonic = monotonic  # 可注入的单调时钟，用于确定性测试。

    async def execute(
        self,
        ssh_session_id: UUID,
        command: str,
        cancelled: asyncio.Event,
    ) -> CommandToolEnvelope:
        """只派发一次执行，与取消竞争，并返回稳定结果信封。"""

        # 1. 只使用本轮冻结的活动会话；派发前再次检查取消。
        owner = self._ssh_sessions.get(ssh_session_id)
        if owner is None or not self._ssh_sessions.is_connected(ssh_session_id):
            return _failure(
                "SSH_SESSION_UNAVAILABLE",
                "The bound SSH session is unavailable.",
            )
        if cancelled.is_set():
            raise AgentCancelled(
                message="the command was cancelled before SSH process creation"
            )

        started_at = self._monotonic()
        process: Any | None = None
        wait_task: asyncio.Task[Any] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        outcome_determined = False
        try:
            # 2. 创建唯一远程命令通道；取得句柄后立即登记到 SSH 所有权注册表。
            create_task = asyncio.ensure_future(
                owner.connection.create_process(command, encoding=None)
            )
            try:
                process = await asyncio.shield(create_task)
            except asyncio.CancelledError:
                # AsyncSSH 可能在返回进程句柄前已发送 exec。
                # 因此暂时保留创建任务，以便取得并接管实际生成的通道；
                # 随后由共用 finally 块关闭该通道。
                creation_result = (
                    await asyncio.gather(create_task, return_exceptions=True)
                )[0]
                if not isinstance(creation_result, BaseException):
                    process = creation_result
                    owner.child_channels.add(process)
                raise
            owner.child_channels.add(process)
            if cancelled.is_set():
                raise AgentCancelled(
                    message="the command was cancelled after SSH process creation"
                )
            # 3. 让命令完成与取消竞争，完成后才把结果标记为已确定。
            wait_task = asyncio.create_task(
                process.wait(check=False, timeout=COMMAND_TIMEOUT_SECONDS)
            )
            cancel_task = asyncio.create_task(cancelled.wait())
            done, _pending = await asyncio.wait(
                {wait_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancelled.is_set():
                raise AgentCancelled(
                    message="the command was cancelled while waiting for SSH completion"
                )

            completed = await wait_task
            outcome_determined = True
            return _envelope_from_bytes(
                policy=self._policy,
                command=command,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_status,
                exit_signal=_signal_name(completed.exit_signal),
                timed_out=False,
                duration_ms=_duration_ms(started_at, self._monotonic()),
            )
        except asyncssh.TimeoutError as error:
            return _envelope_from_bytes(
                policy=self._policy,
                command=command,
                stdout=error.stdout,
                stderr=error.stderr,
                exit_code=None,
                exit_signal=None,
                timed_out=True,
                duration_ms=_duration_ms(started_at, self._monotonic()),
            )
        except AgentCancelled:
            raise
        except (OSError, asyncssh.DisconnectError):
            return _failure(
                "SSH_SESSION_LOST",
                "The SSH transport was lost after command dispatch; remote state is unknown.",
            )
        except Exception:
            return _failure(
                "COMMAND_EXECUTION_ERROR",
                "The command did not produce a determined result.",
            )
        # 4. 回收等待任务；结果不确定时终止并关闭通道，确认关闭后才注销。
        finally:
            if wait_task is not None:
                wait_task.cancel()
            if cancel_task is not None:
                cancel_task.cancel()
            tasks = [task for task in (wait_task, cancel_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None:
                # 所有结果不确定的退出路径都保留注册表所有权，
                # 直到远程通道确认本地关闭。清理失败必须继续抛出，
                # 并让进程保持注册，以便会话关闭时再次清理，
                # 避免因提前注销而丢失资源所有权。
                if not outcome_determined:
                    closed, cleanup_error = await _close_indeterminate_process(process)
                    if closed:
                        owner.child_channels.discard(process)
                    if cleanup_error is not None:
                        raise cleanup_error
                else:
                    owner.child_channels.discard(process)


async def _close_indeterminate_process(
    process: Any,
) -> tuple[bool, BaseException | None]:
    """尝试所有通道清理阶段，并单独报告是否确认关闭。"""

    first_error: BaseException | None = None
    for action in (process.terminate, process.close):
        try:
            action()
        except BaseException as error:
            if first_error is None:
                first_error = error
    closed = False
    try:
        await process.wait_closed()
        closed = True
    except BaseException as error:
        if first_error is None:
            first_error = error
    return closed, first_error


def _duration_ms(started_at: float, ended_at: float) -> int:
    """将非负单调时钟耗时转换为整数毫秒。"""

    return max(0, round((ended_at - started_at) * 1000))


def _signal_name(value: object) -> str | None:
    """将 AsyncSSH 退出信号元组投影为公开信号名称。"""

    if value is None:
        return None
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        return value[0]
    if isinstance(value, str):
        return value
    return None


def _strict_utf8(value: object) -> str:
    """解码 AsyncSSH 二进制输出，不做替换或隐式强制转换。"""

    if not isinstance(value, bytes):
        raise TypeError("command output was not binary")
    return value.decode("utf-8", errors="strict")


def _envelope_from_bytes(
    *,
    policy: AgentContextPolicy = AgentContextPolicy(),
    command: str,
    stdout: object,
    stderr: object,
    exit_code: int | None,
    exit_signal: str | None,
    timed_out: bool,
    duration_ms: int,
) -> CommandToolEnvelope:
    """严格校验输出后构建完成或超时结果信封。"""

    # 1. 严格解码两个输出流；非法 UTF-8 直接产生失败信封。
    try:
        decoded_stdout = _strict_utf8(stdout)
        decoded_stderr = _strict_utf8(stderr)
    except (TypeError, UnicodeDecodeError):
        return _failure(
            "COMMAND_OUTPUT_INVALID_UTF8",
            "Remote command output was not valid UTF-8.",
        )

    # 2. 按后端策略保留首部，同时记录裁剪统计，不把提示混入输出。
    decoded_stdout, stdout_meta = clip_output(decoded_stdout, policy.tool_stdout_max_chars)
    decoded_stderr, stderr_meta = clip_output(decoded_stderr, policy.tool_stderr_max_chars)
    code = "COMMAND_TIMEOUT" if timed_out else "COMMAND_COMPLETED"
    message = (
        "Remote command timed out; remote state is unknown."
        if timed_out
        else "Remote command finished."
    )
    # 3. 统一封装完成或超时结果，保留退出状态、信号及已收集输出。
    return CommandToolEnvelope(
        ok=not timed_out,
        code=code,
        message=message,
        result=CommandExecutionResult(
            command=command,
            exit_code=exit_code,
            exit_signal=exit_signal,
            stdout_truncation=stdout_meta,
            stderr_truncation=stderr_meta,
            stdout=decoded_stdout,
            stderr=decoded_stderr,
            timed_out=timed_out,
            duration_ms=duration_ms,
        ),
    )


def _failure(code: str, message: str) -> CommandToolEnvelope:
    """构建非敏感失败，不声称结果已确定。"""

    return CommandToolEnvelope(
        ok=False,
        code=code,
        message=message,
        result=None,
    )
