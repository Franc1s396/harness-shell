from tests.agent.fakes import FakeSessionRegistry
"""覆盖 Provider 传输变体，不要求完整 SDK schema。"""

from ..storage_support import RepositoryClient, sql

import asyncio

import pytest

from harness_shell_sidecar.agent.model_gateway import (
    _InvocationState,
    _parse_chat_completions_stream,
    _parse_responses_stream,
    _serialize_responses_input,
)
from .fakes import FakeAsyncStream
from .test_model_gateway import RecordingTextSink, responses_config


@pytest.mark.parametrize("variant", ["sparse", "usage", "unknown", "multiple", "finish", "eof"])
def test_chat_accepts_permissive_wire(variant: str) -> None:
    """元数据和结束惯例不得导致合法首个 choice 文本被拒绝。"""
    async def scenario() -> None:
        wire = [{"choices": [{"delta": {"content": "answer"}}]}]
        if variant == "usage":
            wire += [{"choices": [], "usage": {"total_tokens": 5}}]
        elif variant == "unknown":
            wire.insert(0, {"vendor_event": "heartbeat"})
        elif variant == "multiple":
            wire[0]["choices"].append({"delta": {"content": "other"}})
        elif variant == "finish":
            wire += [{"choices": [{"finish_reason": "stop"}]}] * 2
        sink = RecordingTextSink()
        result = await _parse_chat_completions_stream(FakeAsyncStream(wire), sink, _InvocationState())
        assert result.content == "answer"
        assert "".join(sink.deltas) == "answer"
    asyncio.run(scenario())


@pytest.mark.parametrize("tool", [False, True])
def test_responses_streams_draft_then_replaces_with_final_answer(tool: bool) -> None:
    """最终修订应替换已实时显示的文本，包括工具说明。"""
    async def scenario() -> None:
        sink = RecordingTextSink()
        output = [{"type": "message", "content": [{"type": "output_text", "text": "final"}]}]
        if tool:
            output.append({"type": "function_call", "call_id": "call-1", "name": "execute_command", "arguments": "{'command': 'pwd'}"})
        async def stream():
            yield {"type": "response.output_text.delta", "delta": "draft"}
            assert sink.streamed_text == "draft"
            yield {"type": "response.completed", "response": {"output": output}}
        result = await _parse_responses_stream(stream(), responses_config(), sink, _InvocationState())
        assert result.content == "final"
        assert sink.streamed_text == "final"
        assert bool(result.tool_calls) is tool
        _serialize_responses_input(responses_config(), [result])
    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["unknown", "missing_index", "done_only", "final_only", "empty_final", "duplicate", "eof", "reasoning"])
def test_responses_accepts_permissive_wire(variant: str) -> None:
    """稀疏生命周期事件和未知元数据必须保留可见输出。"""
    async def scenario() -> None:
        item = {"type": "message", "id": "msg-1", "content": [{"type": "output_text", "text": "answer"}]}
        delta = {"type": "response.output_text.delta", "item_id": "msg-1", "delta": "answer"}
        done = {"type": "response.output_item.done", "item": item}
        terminal = {"type": "response.completed", "response": {"output": [item]}}
        wire = [delta, done, terminal]
        if variant == "unknown":
            wire.insert(0, {"type": "response.vendor.heartbeat"})
            wire.append({"type": "response.vendor.usage", "tokens": 1})
        elif variant == "done_only":
            wire = [done]
        elif variant == "final_only":
            wire = [terminal]
        elif variant == "empty_final":
            terminal["response"]["output"] = []
        elif variant == "duplicate":
            wire = [delta, done, done, terminal, terminal]
        elif variant == "eof":
            wire = [delta]
        elif variant == "reasoning":
            wire.insert(0, {"type": "response.reasoning_summary_text.delta", "delta": "hidden"})
        config = responses_config()
        sink = RecordingTextSink()
        result = await _parse_responses_stream(FakeAsyncStream(wire), config, sink, _InvocationState())
        assert result.content == "answer"
        assert "".join(sink.deltas) == "answer"
        assert result.tool_calls == []
        assert _serialize_responses_input(config, [result])[0]["content"][0]["text"] == "answer"
    asyncio.run(scenario())


@pytest.mark.parametrize("responses", [False, True])
def test_tool_can_complete_without_delta_lifecycle(responses: bool) -> None:
    """完整工具参数不要求冗余结束元数据。"""
    async def scenario() -> None:
        if responses:
            wire = [{"type": "response.completed", "response": {"output": [{"type": "function_call", "call_id": "call-1", "name": "execute_command", "arguments": {"command": "pwd"}}]}}]
        else:
            wire = [{"choices": [{"delta": {"tool_calls": [{"id": "call-1", "function": {"name": "execute_command", "arguments": {"command": "pwd"}}}]}}]}]
        sink = RecordingTextSink()
        if responses:
            result = await _parse_responses_stream(FakeAsyncStream(wire), responses_config(), sink, _InvocationState())
        else:
            result = await _parse_chat_completions_stream(FakeAsyncStream(wire), sink, _InvocationState())
        assert result.tool_calls == [{"type": "tool_call", "id": "call-1", "name": "execute_command", "args": {"command": "pwd"}}]
        assert sink.deltas == []
    asyncio.run(scenario())


@pytest.mark.parametrize("responses", [False, True])
def test_mixed_tool_turn_displays_commentary_then_updates_final_answer(agent_storage, responses: bool) -> None:
    """运行真实图和服务，确保工具说明可见且最终文本一致。"""
    from harness_shell_sidecar.agent.context import ContextService
    from harness_shell_sidecar.agent.contracts import ApiType, AgentRunStatus
    from harness_shell_sidecar.agent.model_gateway import ModelGateway
    from harness_shell_sidecar.agent.service import AgentService
    from .conftest import valid_api_config_input
    from .fakes import FakeOpenAIClient, RecordingOpenAIClientBuilder, RecordingTurnSink, make_turn_input
    from .test_graph import RecordingExecutor, _run_turn

    async def scenario() -> None:
        call = {"type": "function_call", "call_id": "call-1", "name": "execute_command", "arguments": {"command": "pwd"}}
        if responses:
            first = [{"type": "response.completed", "response": {"output": [
                {"type": "message", "content": [{"type": "output_text", "text": "checking"}]}, call,
            ]}}]
            last = [{"type": "response.completed", "response": {"output": [
                {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
            ]}}]
        else:
            first = [{"choices": [{"delta": {"content": "checking", "tool_calls": [
                {"id": "call-1", "function": {"name": "execute_command", "arguments": {"command": "pwd"}}},
            ]}}]}]
            last = [{"choices": [{"delta": {"content": "answer"}}]}]
        clients = [FakeOpenAIClient(chat_outcomes=[wire], responses_outcomes=[wire]) for wire in (first, last)]
        config = agent_storage.api_configs.create(valid_api_config_input().model_copy(update={"api_type": ApiType.RESPONSES if responses else ApiType.CHAT_COMPLETIONS}))
        sink = RecordingTurnSink()
        def check_commentary_visible() -> None:
            """在真正进入工具执行节点前检查已发布内容。"""
            assert sink.streamed_text == "checking"
        executor = RecordingExecutor(before_execute=check_commentary_visible)
        service = AgentService(agent_storage.database, executor,
            ModelGateway(client_builder=RecordingOpenAIClientBuilder(clients)),
            ContextService(agent_storage.database), lambda _: True, ssh_sessions=FakeSessionRegistry())
        turn = make_turn_input().model_copy(update={"api_config_id": config.api_config_id})
        result = await _run_turn(agent_storage, service, turn, event_sink=sink)
        assert result.status is AgentRunStatus.COMPLETED
        assert result.final_text == "answer" and sink.parts == ["answer"]
        assert ("delta", "checking") in sink.events
        assert ("replace", "answer") in sink.events
        assert executor.calls == [(turn.ssh_session_id, "pwd")]
        history = agent_storage.conversations.load_messages(result.conversation_id)
        assert any(message.content == "checking" and message.tool_calls for message in history if hasattr(message, "tool_calls"))
        assert all(client.closed for client in clients)
    asyncio.run(scenario())


def test_chat_text_arrives_before_stream_finishes() -> None:
    async def scenario() -> None:
        sink = RecordingTextSink()
        async def stream():
            yield {"choices": [{"delta": {"content": "checking"}}]}
            assert sink.streamed_text == "checking"
            yield {"choices": [{"delta": {"content": " now"}}]}
        result = await _parse_chat_completions_stream(stream(), sink, _InvocationState())
        assert result.content == sink.streamed_text == "checking now"
    asyncio.run(scenario())


@pytest.mark.parametrize("responses", [False, True])
def test_final_snapshot_replaces_draft_and_large_text_is_bounded(responses: bool) -> None:
    async def scenario() -> None:
        class BoundedSink(RecordingTextSink):
            """拒绝超出网关分片预算的文本，记录重建后的完整快照。"""
            async def text_replace(self, text: str) -> None:
                assert len(text) <= 4096
                await super().text_replace(text)
            async def text_delta(self, text: str) -> None:
                assert len(text) <= 4096
                await super().text_delta(text)
        sink = BoundedSink()
        final = "new" * 10000
        if responses:
            wire = [{"type": "response.output_text.delta", "delta": "draft"},
                    {"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": final}]}]}}]
            result = await _parse_responses_stream(FakeAsyncStream(wire), responses_config(), sink, _InvocationState())
        else:
            wire = [{"choices": [{"delta": {"content": "draft"}}]}, {"choices": [{"message": {"content": final}}]}]
            result = await _parse_chat_completions_stream(FakeAsyncStream(wire), sink, _InvocationState())
        assert result.content == sink.streamed_text == final
    asyncio.run(scenario())
