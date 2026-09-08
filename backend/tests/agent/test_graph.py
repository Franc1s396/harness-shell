from __future__ import annotations
from tests.agent.fakes import FakeSessionRegistry

from ..storage_support import RepositoryClient, sql

import asyncio
import sqlite3
from contextlib import closing
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

import pytest
import httpx
from langchain_core.messages import AIMessage
from langchain_core.messages.tool import ToolCall
from pydantic import SecretStr

from harness_shell_sidecar.agent.context import ContextService
from harness_shell_sidecar.agent.contracts import (
    AgentRunStatus,
    AgentTurnInput,
    CommandExecutionResult,
    CommandToolEnvelope,
)
from harness_shell_sidecar.agent.graph import AgentGraphDependencies, build_agent_graph
from harness_shell_sidecar.agent.model_gateway import ModelGateway
from harness_shell_sidecar.agent.service import AgentService
from harness_shell_sidecar.agent.tools import CommandSafetyReviewer
from harness_shell_sidecar.telemetry import ConsoleLogFormatter

from .conftest import AgentStorage, valid_api_config_input
from .fakes import (
    FakeModelSequence,
    RecordingTurnSink,
    RecordingSequenceClientBuilder,
    instant_sleep,
    make_tool_call,
    make_turn_input,
)


@dataclass(slots=True)
class RecordingExecutor:
    """记录图派发并返回确定性命令信封。"""

    before_execute: Callable[[], None] | None = None
    failure: Exception | None = None
    stdout: str | None = None
    calls: list[tuple[UUID, str]] = field(default_factory=list)

    async def execute(
        self,
        ssh_session_id: UUID,
        command: str,
        _cancelled: asyncio.Event,
    ) -> CommandToolEnvelope:
        """观察持久化顺序，再返回或抛出固定结果。"""

        if self.before_execute is not None:
            self.before_execute()
        self.calls.append((ssh_session_id, command))
        if self.failure is not None:
            raise self.failure
        from harness_shell_sidecar.agent.executor import _envelope_from_bytes
        output = self.stdout if self.stdout is not None else "/home/test\n" if command == "pwd" else "ok\n"
        return _envelope_from_bytes(command=command, stdout=output.encode(), stderr=b"",
            exit_code=0, exit_signal=None, timed_out=False, duration_ms=1)



def _service(
    agent_storage: AgentStorage,
    model: FakeModelSequence,
    executor: RecordingExecutor,
) -> tuple[AgentService, AgentTurnInput]:
    """围绕确定性模型和 SSH 替身构建真实仓库与图服务。"""

    config = agent_storage.api_configs.create(valid_api_config_input())
    context = ContextService(agent_storage.database)
    gateway = ModelGateway(
        client_builder=RecordingSequenceClientBuilder(model),
        sleep=instant_sleep,
    )
    service = AgentService(agent_storage.database,
        executor,
        gateway,
        context,
        lambda _session_id: True,
    ssh_sessions=FakeSessionRegistry())
    turn = make_turn_input().model_copy(update={"api_config_id": config.api_config_id})
    return service, turn


async def _run_turn(
    agent_storage: AgentStorage,
    service: AgentService,
    turn: AgentTurnInput,
    *,
    api_key: str = "key",
    event_sink: RecordingTurnSink | None = None,
) -> AgentTurnResult:
    """使用 handler 观察到的精确配置快照运行服务。"""

    config = agent_storage.api_configs.get(turn.api_config_id)
    assert config is not None
    return await service.run_turn(
        turn,
        SecretStr(api_key),
        asyncio.Event(),
        expected_config=config,
        event_sink=event_sink or RecordingTurnSink(),
    )


def test_tool_result_returns_to_model_before_final_answer(
    agent_storage: AgentStorage,
) -> None:
    """将配对 ToolMessage 路由回上下文投影后再生成最终文本。"""

    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(
            AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")]),
            AIMessage(content="The remote directory is /home/test."),
        )
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        turn = turn.model_copy(update={"user_message": "where am I?"})
        event_sink = RecordingTurnSink()

        def assert_started_before_execution() -> None:
            """真实图必须先发布状态，再调用远端执行器。"""
            assert event_sink.events[-1] == ("tool_started", {"tool_call_id": "call-1", "tool_name": "execute_command", "arguments": {"command": "pwd"}})

        executor.before_execute = assert_started_before_execution

        result = await _run_turn(
            agent_storage,
            service,
            turn,
            event_sink=event_sink,
        )

        assert result.status is AgentRunStatus.COMPLETED
        assert result.final_text == "The remote directory is /home/test."
        assert model.message_calls[1][-1]["tool_call_id"] == "call-1"
        assert executor.calls == [(turn.ssh_session_id, "pwd")]
        assert event_sink.parts == ["The remote directory is /home/test."]

    asyncio.run(scenario())


def test_model_only_turn_logs_exact_node_pairs_and_route(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """暴露真实成功节点顺序，不序列化图状态。"""

    async def scenario() -> None:
        model = FakeModelSequence([AIMessage(content="done")])
        service, turn = _service(agent_storage, model, RecordingExecutor())
        caplog.set_level(logging.DEBUG, logger="harness_shell_sidecar.agent.graph")

        await _run_turn(agent_storage, service, turn)

        events = [
            (getattr(record, "harness_event", None), record.harness_fields)
            for record in caplog.records
            if getattr(record, "harness_event", "").startswith("agent_")
        ]
        assert [
            (event, fields["node"])
            for event, fields in events
            if "node" in fields
        ] == [
            ("agent_node_started", "load_context"),
            ("agent_node_completed", "load_context"),
            ("agent_node_started", "compact_context"),
            ("agent_node_completed", "compact_context"),
            ("agent_node_started", "prepare_model_context"),
            ("agent_node_completed", "prepare_model_context"),
            ("agent_node_started", "call_model"),
            ("agent_node_completed", "call_model"),
            ("agent_node_started", "return_response"),
            ("agent_node_completed", "return_response"),
        ]
        assert any(
            event == "agent_route_selected"
            and fields["route_source"] == "call_model"
            and fields["route_target"] == "return_response"
            for event, fields in events
        )
        assert all(
            fields["duration_ms"] >= 0
            for event, fields in events
            if event == "agent_node_completed"
        )

    asyncio.run(scenario())


def test_graph_logs_no_message_command_output_or_provider_key(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """将每条图日志限制为 ID、节点元数据和路由。"""

    async def scenario() -> None:
        user_marker = "graph-user-message-marker-1f4b"
        command_marker = "cat /graph-command-marker-2a5c"
        tool_output_marker = "graph-tool-output-marker-3b6d"
        model_output_marker = "graph-model-output-marker-4c7e"
        provider_key_marker = "graph-provider-key-marker-5d8f"
        model = FakeModelSequence(
            [
                AIMessage(
                    content="",
                    tool_calls=[make_tool_call("call-safe-log", command_marker)],
                ),
                AIMessage(content=model_output_marker),
            ]
        )
        executor = RecordingExecutor(stdout=tool_output_marker)
        service, turn = _service(agent_storage, model, executor)
        turn = turn.model_copy(update={"user_message": user_marker})
        caplog.set_level(logging.DEBUG, logger="harness_shell_sidecar.agent.graph")

        await _run_turn(
            agent_storage,
            service,
            turn,
            api_key=provider_key_marker,
        )

        graph_records = [
            record
            for record in caplog.records
            if record.name == "harness_shell_sidecar.agent.graph"
        ]
        assert graph_records
        encoded = "\n".join(
            ConsoleLogFormatter().format(record) for record in graph_records
        )
        for marker in (
            user_marker,
            command_marker,
            tool_output_marker,
            model_output_marker,
            provider_key_marker,
        ):
            assert marker not in encoded

    asyncio.run(scenario())


def test_execute_tool_failure_logs_traceback_and_preserves_result(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常日志保留节点元数据和 traceback，保留现有失败映射。"""

    async def scenario() -> None:
        marker = "graph-executor-failure-marker-6e9a"
        model = FakeModelSequence(
            [
                AIMessage(
                    content="",
                    tool_calls=[make_tool_call("call-failure", "pwd")],
                )
            ]
        )
        executor = RecordingExecutor(failure=RuntimeError(marker))
        service, turn = _service(agent_storage, model, executor)
        caplog.set_level(logging.INFO, logger="harness_shell_sidecar.agent.graph")

        result = await _run_turn(agent_storage, service, turn)

        assert result.status is AgentRunStatus.FAILED
        assert result.error_code == "SIDECAR_RUNTIME_FAILED"
        failed = [
            record
            for record in caplog.records
            if getattr(record, "harness_event", None) == "agent_node_failed"
        ]
        assert len(failed) == 1
        assert failed[0].harness_fields["node"] == "execute_tool"
        assert failed[0].exc_info is not None
        assert failed[0].exc_info[1] is executor.failure
        encoded = ConsoleLogFormatter().format(failed[0])
        assert f"RuntimeError: {marker}" in encoded
        assert "Traceback (most recent call last):" in encoded
        assert "error_code=SIDECAR_RUNTIME_FAILED" in encoded

    asyncio.run(scenario())


def test_provider_failure_body_is_absent_from_full_graph_logs(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """完整图路径不得泄露秘密、Provider 内容或远程内容。"""

    async def scenario() -> None:
        api_key_marker = "provider-key-marker-01"
        user_marker = "user-message-marker-02"
        provider_body_marker = "provider-body-marker-03"
        command_marker = "command-marker-04"
        output_marker = "output-marker-05"
        request = httpx.Request("POST", "https://provider.example/v1/responses")
        response = httpx.Response(
            500,
            request=request,
            json={
                "error": {
                    "message": provider_body_marker,
                    "command": command_marker,
                    "output": output_marker,
                }
            },
        )
        failure = httpx.HTTPStatusError(
            "provider request failed",
            request=request,
            response=response,
        )
        model = FakeModelSequence([failure])
        service, turn = _service(agent_storage, model, RecordingExecutor())
        turn = turn.model_copy(update={"user_message": user_marker})
        caplog.set_level(logging.DEBUG)

        result = await _run_turn(
            agent_storage,
            service,
            turn,
            api_key=api_key_marker,
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error_code == "MODEL_REQUEST_FAILED"
        encoded = "\n".join(
            ConsoleLogFormatter().format(record) for record in caplog.records
        )
        for marker in (
            api_key_marker,
            user_marker,
            provider_body_marker,
            command_marker,
            output_marker,
        ):
            assert marker not in encoded

    asyncio.run(scenario())


def test_ai_tool_call_is_persisted_before_executor_dispatch(
    agent_storage: AgentStorage,
) -> None:
    """远程副作用发生前确保已持久化的 AI 工具决策可见。"""

    async def scenario() -> None:
        model = FakeModelSequence(
            [
                AIMessage(
                    content="",
                    tool_calls=[make_tool_call("call-order", "pwd")],
                ),
                AIMessage(content="done"),
            ]
        )
        observed: list[str] = []

        def inspect_history() -> None:
            """在执行器调用边界精确读取持久化元数据。"""

            rows = sql(agent_storage.database,
                "SELECT message_type FROM agent_messages ORDER BY sequence"
            ).fetchall()
            observed.extend(row[0] for row in rows)

        executor = RecordingExecutor(before_execute=inspect_history)
        service, turn = _service(agent_storage, model, executor)

        await _run_turn(agent_storage, service, turn)

        assert observed == ["HUMAN", "AI"]

    asyncio.run(scenario())


def test_regex_rejection_is_persisted_and_returned_to_model(
    agent_storage: AgentStorage,
) -> None:
    """不调用 SSH，将固定安全拒绝作为配对 ToolMessage 返回。"""

    async def scenario() -> None:
        model = FakeModelSequence(
            [
                AIMessage(
                    content="",
                    tool_calls=[make_tool_call("call-danger", "rm -rf /")],
                ),
                AIMessage(content="I will not run that command."),
            ]
        )
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)

        result = await _run_turn(agent_storage, service, turn)

        tool = model.message_calls[1][-1]
        assert json.loads(tool["content"])["code"] == (
            "COMMAND_REJECTED_DANGEROUS_PATTERN"
        )
        assert executor.calls == []
        assert result.status is AgentRunStatus.COMPLETED

    asyncio.run(scenario())


def test_multiple_tool_calls_execute_none_and_each_gets_paired_error(
    agent_storage: AgentStorage,
) -> None:
    """只计一次循环，同时拒绝并行模型响应中的每个调用。"""

    async def scenario() -> None:
        model = FakeModelSequence(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        make_tool_call("call-1", "pwd"),
                        make_tool_call("call-2", "uname -a"),
                    ],
                ),
                AIMessage(content="I can only execute one command at a time."),
            ]
        )
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)

        result = await _run_turn(agent_storage, service, turn)

        tool_messages = model.message_calls[1][-2:]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call-1",
            "call-2",
        ]
        assert all(
            json.loads(message["content"])["code"]
            == "MULTIPLE_TOOL_CALLS_UNSUPPORTED"
            for message in tool_messages
        )
        assert executor.calls == []
        assert result.react_iteration == 1

    asyncio.run(scenario())


def test_unknown_tool_is_not_executed(
    agent_storage: AgentStorage,
) -> None:
    """为未知工具调用配对错误，并保留循环协议。"""

    async def scenario() -> None:
        unknown = make_tool_call("call-unknown", "pwd")
        unknown["name"] = "unknown_tool"
        model = FakeModelSequence(
            [
                AIMessage(content="", tool_calls=[unknown]),
                AIMessage(content="The tool is unavailable."),
            ]
        )
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)

        await _run_turn(agent_storage, service, turn)

        assert json.loads(model.message_calls[1][-1]["content"])["code"] == "UNKNOWN_TOOL"
        assert executor.calls == []

    asyncio.run(scenario())


def test_128_completed_iterations_may_return_a_final_answer(
    agent_storage: AgentStorage,
) -> None:
    """允许模型在恰好完成 128 次工具循环后结束。"""

    async def scenario() -> None:
        calls = [
            AIMessage(
                content="",
                tool_calls=[make_tool_call(f"call-{index}", "pwd")],
            )
            for index in range(1, 129)
        ]
        model = FakeModelSequence([*calls, AIMessage(content="finished at 128")])
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)

        result = await _run_turn(agent_storage, service, turn)

        assert result.status is AgentRunStatus.COMPLETED
        assert result.react_iteration == 128
        assert result.final_text == "finished at 128"
        assert len(executor.calls) == 128

    asyncio.run(scenario())


def test_129th_tool_call_is_paired_but_never_executed(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """在业务上限处停止，不依赖 LangGraph 递归限制。"""

    async def scenario() -> None:
        model = FakeModelSequence(
            [
                AIMessage(
                    content="",
                    tool_calls=[make_tool_call(f"call-{index}", "pwd")],
                )
                for index in range(1, 130)
            ]
        )
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        caplog.set_level(logging.DEBUG, logger="harness_shell_sidecar.agent.graph")

        result = await _run_turn(agent_storage, service, turn)

        assert result.status is AgentRunStatus.LIMIT_REACHED
        assert result.error_code == "REACT_LIMIT_REACHED"
        assert result.react_iteration == 128
        assert len(executor.calls) == 128
        history = agent_storage.conversations.load_messages(result.conversation_id)
        assert json.loads(history[-1].content)["code"] == "REACT_LIMIT_REACHED"
        assert history[-1].tool_call_id == "call-129"
        graph_records = [
            record
            for record in caplog.records
            if record.name == "harness_shell_sidecar.agent.graph"
        ]
        observed_nodes = {
            record.harness_fields["node"]
            for record in graph_records
            if getattr(record, "harness_event", None) == "agent_node_started"
        }
        assert {
            "check_react_limit",
            "execute_tool",
            "reject_limit",
        } <= observed_nodes
        routes = [
            record.harness_fields
            for record in graph_records
            if getattr(record, "harness_event", None) == "agent_route_selected"
        ]
        assert any(
            fields["route_source"] == "call_model"
            and fields["route_target"] == "check_react_limit"
            for fields in routes
        )
        assert any(
            fields["route_source"] == "check_react_limit"
            and fields["route_target"] == "prepare_tool"
            for fields in routes
        )
        assert any(
            fields["route_source"] == "check_react_limit"
            and fields["route_target"] == "reject_limit"
            for fields in routes
        )

    asyncio.run(scenario())


def test_compiled_graph_uses_explicit_checkpointer(agent_storage: AgentStorage) -> None:
    """图使用调用方显式提供的内存 saver，不创建跨 Run 恢复权威。"""

    model = FakeModelSequence()
    dependencies = AgentGraphDependencies(
        database=agent_storage.database,
        context=ContextService(agent_storage.database),
        gateway=ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=instant_sleep,
        ),
        reviewer=CommandSafetyReviewer(),
        executor=RecordingExecutor(),
    )

    from langgraph.checkpoint.memory import InMemorySaver
    saver = InMemorySaver()
    graph = build_agent_graph(dependencies, checkpointer=saver)

    assert graph.checkpointer is saver


def test_full_turn_never_persists_or_logs_provider_key_sentinel(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一轮结束后扫描持久化行、构建器诊断和日志。"""

    async def scenario() -> None:
        sentinel = "provider-key-sentinel-full-turn-71d4"
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence([AIMessage(content="safe final answer")])
        builder = RecordingSequenceClientBuilder(model)
        service = AgentService(agent_storage.database,
            RecordingExecutor(),
            ModelGateway(client_builder=builder, sleep=instant_sleep),
            ContextService(agent_storage.database),
            lambda _session_id: True,
        ssh_sessions=FakeSessionRegistry())
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        caplog.set_level(logging.DEBUG)

        result = await service.run_turn(
            turn,
            SecretStr(sentinel),
            asyncio.Event(),
            expected_config=config,
            event_sink=RecordingTurnSink(),
        )

        assert result.status is AgentRunStatus.COMPLETED
        with closing(sqlite3.connect(agent_storage.database.path)) as connection:
            durable_dump = "\n".join(connection.iterdump())
        diagnostics = f"{builder.kwargs}:{caplog.text}:{durable_dump}"
        assert sentinel not in diagnostics
        assert str(builder.kwargs["api_key"]) == "**********"

    asyncio.run(scenario())


@pytest.mark.parametrize("main_fails", [False, True])
@pytest.mark.parametrize("tool_loop", [False, True])
def test_compaction_streams_only_main_answer_and_preserves_history(agent_storage: AgentStorage, main_fails: bool, tool_loop: bool) -> None:
    from harness_shell_sidecar.agent.context_summaries import ContextSummaryRepository
    from harness_shell_sidecar.agent.contracts import AgentRunStatus
    from uuid import uuid4
    async def scenario() -> None:
        outcomes = [AIMessage(content="HISTORY SUMMARY ONLY")]
        if tool_loop:
            outcomes.append(AIMessage(content="", tool_calls=[make_tool_call("post-summary", "pwd")]))
        outcomes.append(RuntimeError("provider failure") if main_fails else AIMessage(content="final answer"))
        model = FakeModelSequence(outcomes)
        service, turn = _service(agent_storage, model, RecordingExecutor())
        config = agent_storage.api_configs.get(turn.api_config_id)
        value = valid_api_config_input().model_copy(update={
            "api_key_credential_id": config.api_key_credential_id,
            "context_compaction_threshold_ratio": 0.01})
        agent_storage.api_configs.update(config.api_config_id, value)
        repo = agent_storage.conversations
        conversation = repo.create_conversation()
        for i in range(4):
            run = repo.start_run(conversation, turn.ssh_session_id, config.api_config_id)
            repo.append_messages_atomic(run.agent_run_id, conversation,
                [HumanMessage(content="historical data " * 500), AIMessage(content=f"answer {i}")])
            repo.finish_run(run.agent_run_id, AgentRunStatus.COMPLETED, None)
        before = repo.load_messages(conversation)
        sink = RecordingTurnSink()
        result = await _run_turn(agent_storage, service,
            turn.model_copy(update={"conversation_id": conversation}), event_sink=sink)
        assert result.status == (AgentRunStatus.FAILED if main_fails else AgentRunStatus.COMPLETED)
        assert sink.streamed_text == ("" if main_fails else "final answer")
        assert model.calls == (3 if tool_loop else 2)
        assert repo.load_messages(conversation)[:len(before)] == before
        summary = RepositoryClient(agent_storage.database, ContextSummaryRepository).load(conversation)
        assert summary.covered_through_sequence == 2
        assert "HISTORY SUMMARY ONLY" in str(model.message_calls[1])
    from langchain_core.messages import HumanMessage
    asyncio.run(scenario())


def test_tool_prefix_is_identical_in_database_and_model(agent_storage: AgentStorage) -> None:
    async def scenario() -> None:
        model = FakeModelSequence([AIMessage(content="", tool_calls=[make_tool_call("clip", "pwd")]), AIMessage(content="done")])
        service, turn = _service(agent_storage, model, RecordingExecutor(stdout="x" * 6000 + "OMITTED_SUFFIX"))
        result = await _run_turn(agent_storage, service, turn)
        history = agent_storage.conversations.load_messages(result.conversation_id)
        tool = next(message for message in history if message.type == "tool")
        payload = json.loads(tool.content)
        assert payload["result"]["stdout"] == "x" * 6000
        assert payload["result"]["stdout_truncation"]["omitted_chars"] == 14
        sent = next(message for message in model.message_calls[1] if message["role"] == "tool")
        assert sent["content"] == tool.content
        assert "OMITTED_SUFFIX" not in str(history)
    asyncio.run(scenario())


def test_tool_loop_budget_overflow_never_calls_summary_or_next_model(agent_storage: AgentStorage) -> None:
    from collections.abc import Sequence
    from harness_shell_sidecar.agent.context_budget import ContextBudget
    from harness_shell_sidecar.agent.context_models import AgentContextPolicy, ContextMessage, ContextSummary, TokenEstimate
    from harness_shell_sidecar.agent.contracts import ModelApiConfig
    from harness_shell_sidecar.agent.tokenizer import load_local_encoding, tokenizer_resource_dir

    class ToolOverflowBudget(ContextBudget):
        """模拟大型工具结果，不制造巨大测试载荷。"""
        def estimate(self, config: ModelApiConfig, records: Sequence[ContextMessage],
                     summary: ContextSummary | None) -> TokenEstimate:
            """仅工具后的投影超过实际默认预算。"""
            return TokenEstimate(120000 if any(r.message.type == "tool" for r in records) else 500,
                                 "TOKENIZER_ESTIMATE")

    async def scenario() -> None:
        """覆盖真实图边和持久化终态失败。"""
        model = FakeModelSequence([AIMessage(content="", tool_calls=[make_tool_call("one", "pwd")])])
        config = agent_storage.api_configs.create(valid_api_config_input())
        budget = ToolOverflowBudget(load_local_encoding(tokenizer_resource_dir(), "o200k_base"), AgentContextPolicy())
        service = AgentService(agent_storage.database, RecordingExecutor(),
            ModelGateway(client_builder=RecordingSequenceClientBuilder(model)),
            ContextService(agent_storage.database), lambda _: True, budget=budget, ssh_sessions=FakeSessionRegistry())
        turn = make_turn_input().model_copy(update={"api_config_id": config.api_config_id})
        result = await _run_turn(agent_storage, service, turn)
        assert result.status is AgentRunStatus.FAILED
        assert result.error_code == "CONTEXT_BUDGET_EXCEEDED"
        assert model.calls == 1
        assert sql(agent_storage.database, "SELECT count(*) FROM agent_context_summaries").fetchone() == (0,)
    asyncio.run(scenario())


@pytest.mark.parametrize("calls", [
    [make_tool_call("blocked", "rm -rf /")],
    [{"id": "unknown", "name": "unknown_tool", "args": {}}],
    [{"id": "invalid", "name": "execute_command", "args": {}}],
    [make_tool_call("one", "pwd"), make_tool_call("two", "pwd")],
])
def test_rejected_tools_do_not_publish_execution_status(agent_storage: AgentStorage, calls: list[ToolCall]) -> None:
    """被拒绝的工具不能在 UI 中被呈现为已开始执行。"""
    async def scenario() -> None:
        """运行真实图的拒绝路径，观察状态和执行器调用。"""
        model = FakeModelSequence([AIMessage(content="", tool_calls=calls), AIMessage(content="done")])
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        sink = RecordingTurnSink()
        await _run_turn(agent_storage, service, turn, event_sink=sink)
        assert executor.calls == []
        assert all(name != "tool_started" for name, _ in sink.events)
    asyncio.run(scenario())
