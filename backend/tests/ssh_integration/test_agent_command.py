from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from harness_shell_sidecar.agent.executor import AgentCancelled, SshCommandExecutor


@pytest.mark.parametrize("connector_fixture", ["connect_direct", "connect_proxy"])
def test_agent_command_runs_only_on_bound_real_openssh_session(
    connector_fixture: str,
    request: pytest.FixtureRequest,
    runtime_context,
    lab,
) -> None:
    """Execute on the selected direct or ProxyJump session in the SSH lab."""

    async def scenario() -> None:
        connector: Callable[[], Awaitable[tuple]] = request.getfixturevalue(
            connector_fixture
        )
        connected = await connector()
        status = connected[-1]
        assert status.session_id is not None
        owner = runtime_context.runtime.sessions.get(status.session_id)
        assert owner is not None
        executor = SshCommandExecutor(runtime_context.runtime.sessions)

        envelope = await executor.execute(
            status.session_id,
            "printf 'agent-session:%s' \"$USER\"; printf 'agent-stderr' >&2",
            asyncio.Event(),
        )

        assert envelope.code == "COMMAND_COMPLETED"
        assert envelope.result is not None
        assert envelope.result.exit_code == 0
        expected_user = (
            lab.jump_username
            if connector_fixture == "connect_direct"
            else lab.target_username
        )
        assert envelope.result.stdout == f"agent-session:{expected_user}"
        assert envelope.result.stderr == "agent-stderr"
        assert owner.child_channels == set()

    async def bounded_scenario() -> None:
        """Bound real SSH I/O and always close the isolated runtime."""

        try:
            async with asyncio.timeout(20):
                await scenario()
        finally:
            async with asyncio.timeout(10):
                await runtime_context.close()

    asyncio.run(bounded_scenario())


def test_agent_real_openssh_timeout_closes_bound_channel(
    connect_direct,
    runtime_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the timeout cleanup path against a real non-PTY OpenSSH channel."""

    async def scenario() -> None:
        _profile, status = await connect_direct()
        assert status.session_id is not None
        monkeypatch.setattr(
            "harness_shell_sidecar.agent.executor.COMMAND_TIMEOUT_SECONDS",
            1,
        )
        executor = SshCommandExecutor(runtime_context.runtime.sessions)

        envelope = await executor.execute(
            status.session_id,
            "printf 'before-timeout' >&2; sleep 5",
            asyncio.Event(),
        )

        owner = runtime_context.runtime.sessions.get(status.session_id)
        assert owner is not None
        assert envelope.code == "COMMAND_TIMEOUT"
        assert envelope.result is not None
        assert envelope.result.stderr == "before-timeout"
        assert owner.child_channels == set()

    async def bounded_scenario() -> None:
        try:
            async with asyncio.timeout(15):
                await scenario()
        finally:
            async with asyncio.timeout(10):
                await runtime_context.close()

    asyncio.run(bounded_scenario())


def test_agent_real_openssh_cancellation_closes_bound_channel(
    connect_direct,
    runtime_context,
) -> None:
    """Cancel a real command and retain Session ownership through channel cleanup."""

    async def scenario() -> None:
        _profile, status = await connect_direct()
        assert status.session_id is not None
        owner = runtime_context.runtime.sessions.get(status.session_id)
        assert owner is not None
        executor = SshCommandExecutor(runtime_context.runtime.sessions)
        cancelled = asyncio.Event()
        execution = asyncio.create_task(
            executor.execute(status.session_id, "sleep 30", cancelled)
        )
        while not owner.child_channels:
            await asyncio.sleep(0)
        cancelled.set()

        with pytest.raises(AgentCancelled):
            await execution

        assert owner.child_channels == set()

    async def bounded_scenario() -> None:
        try:
            async with asyncio.timeout(15):
                await scenario()
        finally:
            async with asyncio.timeout(10):
                await runtime_context.close()

    asyncio.run(bounded_scenario())
