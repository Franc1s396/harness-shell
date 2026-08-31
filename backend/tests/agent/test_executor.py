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
    """Controllable non-PTY SSH process used to verify executor ownership."""

    def __init__(self, outcome: object, *, cleanup_failure: str | None = None) -> None:
        """Store one result, exception, or wait gate as the process outcome."""

        self.outcome = outcome  # Value returned or raised by wait().
        self.cleanup_failure = cleanup_failure  # Injected cleanup phase failure.
        self.terminated = False  # Whether executor sent remote termination.
        self.closed = False  # Whether executor closed the local channel.
        self.wait_closed_called = False  # Whether final channel wait was attempted.
        self.waited_closed = False  # Whether local channel cleanup completed.
        self.wait_started = asyncio.Event()  # Signals registration before completion.

    async def wait(self, *, check: bool, timeout: int) -> object:
        """Return or raise the configured result after recording wait options."""

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
        """Record closure of the local SSH exec channel."""

        self.closed = True
        if self.cleanup_failure == "close":
            raise RuntimeError("close failed")

    def terminate(self) -> None:
        """Record the explicit remote termination sent before channel close."""

        self.terminated = True
        if self.cleanup_failure == "terminate":
            raise RuntimeError("terminate failed")

    async def wait_closed(self) -> None:
        """Record completion of local channel cleanup."""

        self.wait_closed_called = True
        if self.cleanup_failure == "wait_closed":
            raise RuntimeError("wait_closed failed")
        self.waited_closed = True


@dataclass(slots=True)
class FakeCommandConnection:
    """Return one fixed process and record exact non-PTY dispatch calls."""

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
        """Initialize an empty dispatch record."""

        self.calls.clear()

    async def create_process(
        self, command: str, **options: object
    ) -> FakeCommandProcess:
        """Record the raw command and binary encoding option."""

        self.calls.append((command, options))
        self.create_started.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        return self.process

    def close(self) -> None:
        """Satisfy the SSH registry connection ownership contract."""

    async def wait_closed(self) -> None:
        """Satisfy the SSH registry connection ownership contract."""


def _completed(
    *,
    stdout: bytes = b"out",
    stderr: bytes = b"",
    exit_status: int | None = 0,
    exit_signal: tuple[str, bool, str, str] | None = None,
) -> SimpleNamespace:
    """Build the subset of AsyncSSH completed-process data consumed by executor."""

    return SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        exit_status=exit_status,
        exit_signal=exit_signal,
    )


def _clock(*values: float) -> Any:
    """Return a deterministic monotonic clock over the supplied values."""

    iterator: Iterator[float] = iter(values)
    return lambda: next(iterator)


def _registered(
    process: FakeCommandProcess,
    *,
    create_gate: asyncio.Event | None = None,
) -> tuple[SshSessionRegistry, UUID, FakeCommandConnection]:
    """Register one fake live SSH owner and return its stable session identity."""

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
    """Fail before dispatch when the frozen SSH session is unavailable."""

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
    """Observe cancellation before the SSH dispatch boundary and execute nothing."""

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
    """Use only the selected owner and treat nonzero remote exit as determined."""

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
    """Return strict partial output after one timed-out dispatch and close channel."""

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
    """Raise stable cancellation only after closing and unregistering the channel."""

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
    """Reject non-UTF-8 output instead of silently replacing bytes."""

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
    """Classify a transport loss once without replaying an ambiguous command."""

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
    """Clean up an owned channel before mapping an unexpected wait failure."""

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
    """Retain ownership until cleanup when the executor coroutine is cancelled."""

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
    """Retain ownership when exec dispatch precedes delayed process-handle delivery."""

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
    """Attempt every cleanup phase and retain ownership until closure is confirmed."""

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
