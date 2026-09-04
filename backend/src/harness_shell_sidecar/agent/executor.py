"""Execute one reviewed command on its frozen live SSH session."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import asyncssh

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry

from .contracts import CommandExecutionResult, CommandToolEnvelope


COMMAND_TIMEOUT_SECONDS = 30


class AgentCancelled(RuntimeError):
    """Interrupt graph execution after the active local channel is cleaned up."""

    def __init__(
        self,
        error_code: str = "AGENT_CANCELLED",
        message: str = "the Agent operation was cancelled",
    ) -> None:
        """Store the stable cancellation code and reviewed lifecycle reason."""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # Stable run-level cancellation code.
        self.safe_message = message  # Cancellation point without command content.


class SshCommandExecutor:
    """Own non-PTY AsyncSSH exec channels through the existing session registry."""

    def __init__(
        self,
        ssh_sessions: SshSessionRegistry,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the authoritative live-session registry and elapsed-time clock."""

        self._ssh_sessions = ssh_sessions  # Resolves the frozen run session only.
        self._monotonic = monotonic  # Injectable monotonic clock for deterministic tests.

    async def execute(
        self,
        ssh_session_id: UUID,
        command: str,
        cancelled: asyncio.Event,
    ) -> CommandToolEnvelope:
        """Dispatch once, race cancellation, and return a stable result envelope."""

        owner = self._ssh_sessions.get(ssh_session_id)
        if owner is None:
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
            create_task = asyncio.ensure_future(
                owner.connection.create_process(command, encoding=None)
            )
            try:
                process = await asyncio.shield(create_task)
            except asyncio.CancelledError:
                # AsyncSSH may have sent exec before returning its process handle.
                # Keep the creation task alive long enough to acquire and own any
                # resulting channel; the common finally block then closes it.
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
        finally:
            if wait_task is not None:
                wait_task.cancel()
            if cancel_task is not None:
                cancel_task.cancel()
            tasks = [task for task in (wait_task, cancel_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None:
                # Every indeterminate exit path retains registry ownership until
                # the remote channel has acknowledged local closure. Cleanup
                # failures intentionally escape and leave the process registered
                # for session shutdown to retry instead of losing ownership.
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
    """Attempt every channel cleanup phase and report confirmed closure separately."""

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
    """Convert a non-negative monotonic elapsed time to integer milliseconds."""

    return max(0, round((ended_at - started_at) * 1000))


def _signal_name(value: object) -> str | None:
    """Project AsyncSSH's exit-signal tuple into the public signal name."""

    if value is None:
        return None
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        return value[0]
    if isinstance(value, str):
        return value
    return None


def _strict_utf8(value: object) -> str:
    """Decode binary AsyncSSH output without replacement or implicit coercion."""

    if not isinstance(value, bytes):
        raise TypeError("command output was not binary")
    return value.decode("utf-8", errors="strict")


def _envelope_from_bytes(
    *,
    command: str,
    stdout: object,
    stderr: object,
    exit_code: int | None,
    exit_signal: str | None,
    timed_out: bool,
    duration_ms: int,
) -> CommandToolEnvelope:
    """Build a completion or timeout envelope after strict output validation."""

    try:
        decoded_stdout = _strict_utf8(stdout)
        decoded_stderr = _strict_utf8(stderr)
    except (TypeError, UnicodeDecodeError):
        return _failure(
            "COMMAND_OUTPUT_INVALID_UTF8",
            "Remote command output was not valid UTF-8.",
        )

    code = "COMMAND_TIMEOUT" if timed_out else "COMMAND_COMPLETED"
    message = (
        "Remote command timed out; remote state is unknown."
        if timed_out
        else "Remote command finished."
    )
    return CommandToolEnvelope(
        ok=not timed_out,
        code=code,
        message=message,
        result=CommandExecutionResult(
            command=command,
            exit_code=exit_code,
            exit_signal=exit_signal,
            stdout=decoded_stdout,
            stderr=decoded_stderr,
            timed_out=timed_out,
            duration_ms=duration_ms,
        ),
    )


def _failure(code: str, message: str) -> CommandToolEnvelope:
    """Build a non-sensitive failure without claiming a determined result."""

    return CommandToolEnvelope(
        ok=False,
        code=code,
        message=message,
        result=None,
    )
