from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage
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

from .conftest import AgentStorage, valid_api_config_input
from .fakes import (
    FakeModelSequence,
    RecordingModelBuilder,
    instant_sleep,
    make_tool_call,
    make_turn_input,
)


@dataclass(slots=True)
class RecordingExecutor:
    """Record graph dispatches and return deterministic command envelopes."""

    before_execute: Callable[[], None] | None = None
    failure: Exception | None = None
    calls: list[tuple[UUID, str]] = field(default_factory=list)

    async def execute(
        self,
        ssh_session_id: UUID,
        command: str,
        _cancelled: asyncio.Event,
    ) -> CommandToolEnvelope:
        """Observe persistence ordering, then return or raise one fixed outcome."""

        if self.before_execute is not None:
            self.before_execute()
        self.calls.append((ssh_session_id, command))
        if self.failure is not None:
            raise self.failure
        return CommandToolEnvelope(
            ok=True,
            code="COMMAND_COMPLETED",
            message="Remote command finished.",
            result=CommandExecutionResult(
                command=command,
                exit_code=0,
                exit_signal=None,
                stdout="/home/test\n" if command == "pwd" else "ok\n",
                stderr="",
                timed_out=False,
                duration_ms=1,
            ),
        )


def _service(
    agent_storage: AgentStorage,
    model: FakeModelSequence,
    executor: RecordingExecutor,
) -> tuple[AgentService, AgentTurnInput]:
    """Build a real repository/graph service around deterministic model and SSH fakes."""

    config = agent_storage.api_configs.create(valid_api_config_input())
    context = ContextService(agent_storage.conversations)
    gateway = ModelGateway(
        model_builder=RecordingModelBuilder(model),
        sleep=instant_sleep,
    )
    service = AgentService(
        agent_storage.api_configs,
        agent_storage.conversations,
        executor,
        gateway,
        context,
        lambda _session_id: True,
    )
    turn = make_turn_input().model_copy(update={"api_config_id": config.api_config_id})
    return service, turn


async def _run_turn(
    agent_storage: AgentStorage,
    service: AgentService,
    turn: AgentTurnInput,
) -> AgentTurnResult:
    """Run through the service with the exact handler-observed config snapshot."""

    config = agent_storage.api_configs.get(turn.api_config_id)
    assert config is not None
    return await service.run_turn(
        turn,
        SecretStr("key"),
        asyncio.Event(),
        expected_config=config,
    )


def test_tool_result_returns_to_model_before_final_answer(
    agent_storage: AgentStorage,
) -> None:
    """Route a paired ToolMessage back through trim_context before final text."""

    async def scenario() -> None:
        model = FakeModelSequence()
        model.queue(
            AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")]),
            AIMessage(content="The remote directory is /home/test."),
        )
        executor = RecordingExecutor()
        service, turn = _service(agent_storage, model, executor)
        turn = turn.model_copy(update={"user_message": "where am I?"})

        result = await _run_turn(agent_storage, service, turn)

        assert result.status is AgentRunStatus.COMPLETED
        assert result.final_text == "The remote directory is /home/test."
        assert model.message_calls[1][-1].tool_call_id == "call-1"
        assert executor.calls == [(turn.ssh_session_id, "pwd")]

    asyncio.run(scenario())


def test_ai_tool_call_is_persisted_before_executor_dispatch(
    agent_storage: AgentStorage,
) -> None:
    """Make the durable AI tool decision visible before any remote side effect."""

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
            """Read persisted metadata at the exact executor call boundary."""

            rows = agent_storage.database.execute(
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
    """Return the fixed safety rejection as a paired ToolMessage without SSH."""

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
        assert json.loads(tool.content)["code"] == (
            "COMMAND_REJECTED_DANGEROUS_PATTERN"
        )
        assert executor.calls == []
        assert result.status is AgentRunStatus.COMPLETED

    asyncio.run(scenario())


def test_multiple_tool_calls_execute_none_and_each_gets_paired_error(
    agent_storage: AgentStorage,
) -> None:
    """Count one loop while rejecting every call in a parallel model response."""

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
        assert [message.tool_call_id for message in tool_messages] == [
            "call-1",
            "call-2",
        ]
        assert all(
            json.loads(message.content)["code"]
            == "MULTIPLE_TOOL_CALLS_UNSUPPORTED"
            for message in tool_messages
        )
        assert executor.calls == []
        assert result.react_iteration == 1

    asyncio.run(scenario())


def test_unknown_tool_is_not_executed(
    agent_storage: AgentStorage,
) -> None:
    """Pair an unknown tool call with an error while preserving the loop protocol."""

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

        assert json.loads(model.message_calls[1][-1].content)["code"] == "UNKNOWN_TOOL"
        assert executor.calls == []

    asyncio.run(scenario())


def test_128_completed_iterations_may_return_a_final_answer(
    agent_storage: AgentStorage,
) -> None:
    """Allow the model to finish after exactly 128 completed tool loops."""

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
) -> None:
    """Stop at the business limit without relying on LangGraph recursion limits."""

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

        result = await _run_turn(agent_storage, service, turn)

        assert result.status is AgentRunStatus.LIMIT_REACHED
        assert result.error_code == "REACT_LIMIT_REACHED"
        assert result.react_iteration == 128
        assert len(executor.calls) == 128
        history = agent_storage.conversations.load_messages(result.conversation_id)
        assert json.loads(history[-1].content)["code"] == "REACT_LIMIT_REACHED"
        assert history[-1].tool_call_id == "call-129"

    asyncio.run(scenario())


def test_compiled_graph_has_no_checkpointer(agent_storage: AgentStorage) -> None:
    """Keep SQLite conversation storage as the sole recovery authority."""

    model = FakeModelSequence()
    dependencies = AgentGraphDependencies(
        conversations=agent_storage.conversations,
        context=ContextService(agent_storage.conversations),
        gateway=ModelGateway(
            model_builder=RecordingModelBuilder(model),
            sleep=instant_sleep,
        ),
        reviewer=CommandSafetyReviewer(),
        executor=RecordingExecutor(),
    )

    graph = build_agent_graph(dependencies)

    assert graph.checkpointer is None


def test_full_turn_never_persists_or_logs_provider_key_sentinel(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scan durable rows, Trace storage, builder diagnostics, and logs after a turn."""

    async def scenario() -> None:
        sentinel = "provider-key-sentinel-full-turn-71d4"
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence([AIMessage(content="safe final answer")])
        builder = RecordingModelBuilder(model)
        service = AgentService(
            agent_storage.api_configs,
            agent_storage.conversations,
            RecordingExecutor(),
            ModelGateway(model_builder=builder, sleep=instant_sleep),
            ContextService(agent_storage.conversations),
            lambda _session_id: True,
        )
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        caplog.set_level(logging.DEBUG)

        result = await service.run_turn(
            turn,
            SecretStr(sentinel),
            asyncio.Event(),
            expected_config=config,
        )

        assert result.status is AgentRunStatus.COMPLETED
        durable_dump = "\n".join(agent_storage.database.connection.iterdump())
        trace_rows = agent_storage.database.execute(
            "SELECT * FROM trace_spans"
        ).fetchall()
        diagnostics = f"{builder.kwargs}:{caplog.text}:{trace_rows}:{durable_dump}"
        assert sentinel not in diagnostics
        assert str(builder.kwargs["api_key"]) == "**********"

    asyncio.run(scenario())
