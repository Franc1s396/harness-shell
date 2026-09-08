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
    """在 SSH Lab 选定直连或 ProxyJump 会话上执行。"""

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
        """约束真实 SSH I/O，并始终关闭独立运行时。"""

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
    """使用真实非 PTY OpenSSH 通道覆盖超时清理路径。"""

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
    """取消真实命令，在通道清理期间保留 Session 所有权。"""

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


# 此夹具为 Agent 模型历史提供独立临时数据库；SSH 仍归本文件 Runtime owner。
from tests.agent.conftest import agent_storage


@pytest.mark.parametrize("decision", ["reject", "approve"])
def test_hitl_gates_real_openssh_mutation(connect_direct, runtime_context, agent_storage, decision: str) -> None:
    """容器唯一 marker 在 pending/拒绝时不存在，通过后仅写入一次。"""
    from uuid import uuid4
    from langchain_core.messages import AIMessage
    from tests.agent.fakes import FakeModelSequence, RecordingTurnSink, make_tool_call
    from tests.agent.test_graph import _service, _run_turn
    from tests.agent.test_approvals import decision_for
    from harness_shell_sidecar.agent.contracts import AgentRunStatus

    async def scenario() -> None:
        _profile, status = await connect_direct()
        session_id = status.session_id
        assert session_id is not None
        owner = runtime_context.runtime.sessions.get(session_id)
        assert owner is not None
        marker = f"/tmp/harness-hitl-{uuid4().hex}"
        command = f"printf x >> {marker}"
        model = FakeModelSequence()
        model.queue(AIMessage(content="Create marker", tool_calls=[make_tool_call("change", command)]),
                    AIMessage(content="Inspect", tool_calls=[make_tool_call("read", "pwd")]),
                    AIMessage(content="Done"))
        executor = SshCommandExecutor(runtime_context.runtime.sessions)
        service, turn = _service(agent_storage, model, executor)
        service._ssh_sessions = runtime_context.runtime.sessions
        service._session_is_available = runtime_context.runtime.sessions.is_connected
        turn = turn.model_copy(update={"ssh_session_id": session_id})
        sink = RecordingTurnSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        try:
            await asyncio.wait_for(sink.approval_ready.wait(), 5)
            assert (await owner.connection.run(f"test ! -e {marker}", check=False)).exit_status == 0
            pending = sink.approval_requests[0]
            assert pending.arguments.command == command
            service.decide_approval(pending.approval_id, decision_for(pending, decision))
            result = await asyncio.wait_for(task, 5)
            assert result.status == AgentRunStatus.COMPLETED
            if decision == "approve":
                assert (await owner.connection.run(f"cat {marker}", check=True)).stdout == "x"
            else:
                assert (await owner.connection.run(f"test ! -e {marker}", check=False)).exit_status == 0
            assert not service._approvals._entries
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await owner.connection.run(f"rm -f -- {marker}", check=True)

    async def bounded_scenario() -> None:
        try:
            async with asyncio.timeout(25):
                await scenario()
        finally:
            async with asyncio.timeout(10):
                await runtime_context.close()
    asyncio.run(bounded_scenario())
