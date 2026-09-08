"""真实 LangGraph 暂停/恢复与一次命令派发。"""

import asyncio

from langchain_core.messages import AIMessage

from harness_shell_sidecar.agent.approval_models import ApprovalDecision
from harness_shell_sidecar.agent.contracts import AgentRunStatus
from .conftest import AgentStorage
from .fakes import FakeModelSequence, RecordingTurnSink, make_tool_call
from .test_graph import RecordingExecutor, _run_turn, _service


def test_mutation_waits_and_rejection_returns_to_model(agent_storage: AgentStorage) -> None:
    async def scenario() -> None:
        """暂停期间零执行，拒绝后仍允许后续只读步骤。"""
        model = FakeModelSequence()
        model.queue(AIMessage(content="Prepare change", tool_calls=[make_tool_call("change", "touch /tmp/hitl")]),
                    AIMessage(content="Check instead", tool_calls=[make_tool_call("read", "pwd")]),
                    AIMessage(content="Done"))
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sink = RecordingTurnSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        try:
            await asyncio.wait_for(sink.approval_ready.wait(), 2)
            assert executor.calls == []
            request = sink.approval_requests[0]
            service.decide_approval(request.approval_id, ApprovalDecision(
                conversation_id=request.conversation_id, agent_run_id=request.agent_run_id,
                ssh_session_id=request.ssh_session_id, tool_call_id=request.tool_call_id, decision="reject"))
            result = await asyncio.wait_for(task, 2)
            assert result.status == AgentRunStatus.COMPLETED
            assert [command for _, command in executor.calls] == ["pwd"]
            import json
            rejection = model.message_calls[1][-1]
            assert rejection["tool_call_id"] == "change"
            assert json.loads(rejection["content"])["code"] == "COMMAND_REJECTED_BY_USER"
            assert len([event for event in sink.events if event[0] == "started"]) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_approval_executes_once_and_releases_checkpoint(agent_storage: AgentStorage, monkeypatch) -> None:
    """重复通过只执行一次，Run 结束清空所有 checkpoint 与授权。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_shell_sidecar.agent import service as service_module
    from .test_approvals import decision_for
    savers = []

    def make_saver(**kwargs):
        saver = InMemorySaver(**kwargs)
        savers.append(saver)
        return saver

    monkeypatch.setattr(service_module, "InMemorySaver", make_saver)

    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(AIMessage(content="Change", tool_calls=[make_tool_call("change", "touch /tmp/hitl")]),
                    AIMessage(content="Done"))
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sink = RecordingTurnSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        try:
            await asyncio.wait_for(sink.approval_ready.wait(), 2)
            assert not task.done() and not executor.calls
            request = sink.approval_requests[0]
            assert savers[0].storage
            for _ in range(2):
                service.decide_approval(request.approval_id, decision_for(request))
            result = await asyncio.wait_for(task, 2)
            assert result.status == AgentRunStatus.COMPLETED
            assert executor.calls == [(request.ssh_session_id, request.arguments.command)]
            assert not service._approvals._entries
            assert not savers[0].storage and not savers[0].writes and not savers[0].blobs
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_cancellation_while_waiting_never_executes(agent_storage: AgentStorage) -> None:
    """取消原 worker 后释放无限等待和授权，绝不派发命令。"""
    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(AIMessage(content="Change", tool_calls=[make_tool_call("change", "touch /tmp/hitl")]))
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sink = RecordingTurnSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        await asyncio.wait_for(sink.approval_ready.wait(), 2)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert not executor.calls
        assert not service._approvals._entries
    asyncio.run(scenario())


def test_resolution_publication_failure_never_executes(agent_storage: AgentStorage) -> None:
    """审核已记录但 SSE 发布失败时不可越过 UI 生命周期继续执行。"""
    from .test_approvals import decision_for

    class FailedResolutionSink(RecordingTurnSink):
        """模拟原流在发布已决定事件时断开。"""
        async def approval_resolved(self, resolution, tool_call_id):
            raise RuntimeError("stream publication failed")

    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(AIMessage(content="Change", tool_calls=[make_tool_call("change", "touch /tmp/hitl")]))
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sink = FailedResolutionSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        await asyncio.wait_for(sink.approval_ready.wait(), 2)
        request = sink.approval_requests[0]
        service.decide_approval(request.approval_id, decision_for(request))
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert not executor.calls and not service._approvals._entries
    asyncio.run(scenario())


def test_bound_session_loss_invalidates_pending_approval(agent_storage: AgentStorage) -> None:
    """原连接断开唤醒无限审核等待，释放授权且不派发。"""
    from .fakes import FakeSessionRegistry

    class DisconnectingSessions(FakeSessionRegistry):
        """由测试精确控制传输关闭通知。"""
        def __init__(self) -> None:
            self.closed = asyncio.Event()  # 真实传输关闭通知的替身。

        async def wait_unavailable(self, session_id) -> None:
            await self.closed.wait()

    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(AIMessage(content="Change", tool_calls=[make_tool_call("change", "touch /tmp/hitl")]))
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sessions = DisconnectingSessions()
        service._ssh_sessions = sessions
        service._session_is_available = lambda _session_id: not sessions.closed.is_set()
        sink = RecordingTurnSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        await asyncio.wait_for(sink.approval_ready.wait(), 2)
        sessions.closed.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert not executor.calls and not service._approvals._entries
        assert any(event[0] == "failed" for event in sink.events)
    asyncio.run(scenario())


def test_each_new_mutation_requires_a_new_approval(agent_storage: AgentStorage) -> None:
    """上一调用通过不能授权后续不同变更；第二次拒绝仍返回模型。"""
    from .test_approvals import decision_for
    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(AIMessage(content="First", tool_calls=[make_tool_call("one", "touch /tmp/one")]),
                    AIMessage(content="Second", tool_calls=[make_tool_call("two", "touch /tmp/two")]), AIMessage(content="Done"))
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sink = RecordingTurnSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        try:
            await asyncio.wait_for(sink.approval_ready.wait(), 2)
            first = sink.approval_requests[0]
            sink.approval_ready.clear()
            service.decide_approval(first.approval_id, decision_for(first))
            await asyncio.wait_for(sink.approval_ready.wait(), 2)
            second = sink.approval_requests[1]
            assert first.approval_id != second.approval_id
            assert [command for _, command in executor.calls] == ["touch /tmp/one"]
            service.decide_approval(second.approval_id, decision_for(second, "reject"))
            assert (await asyncio.wait_for(task, 2)).status == AgentRunStatus.COMPLETED
            assert [command for _, command in executor.calls] == ["touch /tmp/one"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_checkpoint_cleanup_failure_does_not_replace_cancellation(agent_storage: AgentStorage, monkeypatch) -> None:
    """次生清理异常记录后保留最先发生的取消，并仍清理授权表。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_shell_sidecar.agent import service as service_module
    class FailingCleanupSaver(InMemorySaver):
        """实际清理后报告注入的 cleanup 故障。"""
        def delete_thread(self, thread_id: str) -> None:
            super().delete_thread(thread_id)
            raise RuntimeError("injected checkpoint cleanup failure")
    monkeypatch.setattr(service_module, "InMemorySaver", FailingCleanupSaver)
    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(AIMessage(content="Change", tool_calls=[make_tool_call("one", "touch /tmp/test")]))
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sink = RecordingTurnSink()
        task = asyncio.create_task(_run_turn(agent_storage, service, turn, event_sink=sink))
        await asyncio.wait_for(sink.approval_ready.wait(), 2)
        task.cancel()
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert isinstance(result[0], asyncio.CancelledError)
        assert not service._approvals._entries and not executor.calls
    asyncio.run(scenario())
