"""Exercise provider wire variations without requiring a complete SDK schema."""

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
    """Metadata and finish conventions must not reject valid first-choice text."""
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
def test_responses_publishes_only_resolved_final_answer(tool: bool) -> None:
    """Final replacement and tool commentary must never leak provisional text."""
    async def scenario() -> None:
        sink = RecordingTextSink()
        output = [{"type": "message", "content": [{"type": "output_text", "text": "final"}]}]
        if tool:
            output.append({"type": "function_call", "call_id": "call-1", "name": "execute_command", "arguments": "{'command': 'pwd'}"})
        async def stream():
            yield {"type": "response.output_text.delta", "delta": "draft"}
            assert sink.deltas == []
            yield {"type": "response.completed", "response": {"output": output}}
        result = await _parse_responses_stream(stream(), responses_config(), sink, _InvocationState())
        assert result.content == "final"
        assert sink.deltas == ([] if tool else ["final"])
        assert bool(result.tool_calls) is tool
        _serialize_responses_input(responses_config(), [result])
    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["unknown", "missing_index", "done_only", "final_only", "empty_final", "duplicate", "eof", "reasoning"])
def test_responses_accepts_permissive_wire(variant: str) -> None:
    """Sparse lifecycle events and unknown metadata must preserve visible output."""
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
    """Complete tool arguments do not require redundant finish metadata."""
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
def test_mixed_tool_turn_preserves_history_without_publishing_commentary(agent_storage, responses: bool) -> None:
    """Run the real graph/service so buffered tool text cannot break final SSE checks."""
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
        executor = RecordingExecutor()
        service = AgentService(agent_storage.api_configs, agent_storage.conversations, executor,
            ModelGateway(client_builder=RecordingOpenAIClientBuilder(clients)),
            ContextService(agent_storage.conversations), lambda _: True)
        turn = make_turn_input().model_copy(update={"api_config_id": config.api_config_id})
        sink = RecordingTurnSink()
        result = await _run_turn(agent_storage, service, turn, event_sink=sink)
        assert result.status is AgentRunStatus.COMPLETED
        assert result.final_text == "answer" and sink.parts == ["answer"]
        assert executor.calls == [(turn.ssh_session_id, "pwd")]
        history = agent_storage.conversations.load_messages(result.conversation_id)
        assert any(message.content == "checking" and message.tool_calls for message in history if hasattr(message, "tool_calls"))
        assert all(client.closed for client in clients)
    asyncio.run(scenario())
