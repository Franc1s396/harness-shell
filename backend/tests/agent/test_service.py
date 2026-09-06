from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from harness_shell_sidecar.agent.context import ContextService
from harness_shell_sidecar.agent.contracts import (
    AgentRun,
    AgentRunStatus,
    AgentTurnInput,
    AgentTurnResult,
    CommandToolEnvelope,
)
from harness_shell_sidecar.agent.model_gateway import ModelGateway
from harness_shell_sidecar.agent import service as agent_service_module
from harness_shell_sidecar.agent.graph import CommandExecutor
from harness_shell_sidecar.agent.service import AgentService, AgentServiceError
from harness_shell_sidecar.runtime.models import MAX_JSON_BODY_BYTES

from .conftest import AgentStorage, valid_api_config_input
from .fakes import (
    CancellationAwareModel,
    FakeModelSequence,
    RecordingSequenceClientBuilder,
    RecordingTurnSink,
    instant_sleep,
    make_tool_call,
    make_turn_input,
)
from .test_graph import RecordingExecutor


def _run_lifecycle_records(
    caplog: pytest.LogCaptureFixture,
    agent_run_id: UUID,
) -> list[logging.LogRecord]:
    """按发出顺序返回持久化 Run 生命周期记录。"""

    return [
        record
        for record in caplog.records
        if getattr(record, "harness_fields", {}).get("agent_run_id")
        == str(agent_run_id)
        and getattr(record, "harness_event", "").startswith("agent_run_")
    ]


@dataclass(slots=True)
class BlockingExecutor:
    """阻塞工具执行，并暴露外层取消是否使其结束。"""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(
        self,
        _ssh_session_id: UUID,
        _command: str,
        _cancelled: asyncio.Event,
    ) -> CommandToolEnvelope:
        """等待释放或取消，并始终报告结束。"""

        self.started.set()
        try:
            await self.release.wait()
            raise AssertionError("blocking executor should be cancelled")
        finally:
            self.stopped.set()


def _service(
    agent_storage: AgentStorage,
    model: FakeModelSequence,
    executor: CommandExecutor,
    session_is_available: Callable[[UUID], bool] = lambda _session_id: True,
) -> AgentService:
    """使用真实存储和确定性远程边界构建 AgentService。"""

    return AgentService(
        agent_storage.api_configs,
        agent_storage.conversations,
        executor,
        ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=instant_sleep,
        ),
        ContextService(agent_storage.conversations),
        session_is_available,
    )


async def _run_turn(
    agent_storage: AgentStorage,
    service: AgentService,
    turn: AgentTurnInput,
    key: str,
    cancelled: asyncio.Event,
    event_sink: RecordingTurnSink | None = None,
) -> AgentTurnResult:
    """使用 handler 观察到的精确配置快照执行。"""

    config = agent_storage.api_configs.get(turn.api_config_id)
    assert config is not None
    return await service.run_turn(
        turn,
        SecretStr(key),
        cancelled,
        expected_config=config,
        event_sink=event_sink or RecordingTurnSink(),
    )


def test_model_failure_marks_run_failed_exactly_once(
    agent_storage: AgentStorage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """将模型异常转换为一次 Run 终态转换。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence([RuntimeError("model failed")])
        service = _service(agent_storage, model, RecordingExecutor())
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        real_finish = agent_storage.conversations.finish_run
        statuses: list[AgentRunStatus] = []

        def count_finish(
            agent_run_id: UUID,
            status: AgentRunStatus,
            error_code: str | None,
        ) -> AgentRun:
            """委托真实仓库执行时记录终态转换。"""

            statuses.append(status)
            return real_finish(agent_run_id, status, error_code)

        monkeypatch.setattr(agent_storage.conversations, "finish_run", count_finish)
        caplog.set_level(logging.INFO, logger="harness_shell_sidecar.agent.service")
        event_sink = RecordingTurnSink()

        result = await _run_turn(
            agent_storage,
            service,
            turn,
            "key",
            asyncio.Event(),
            event_sink,
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error_code == "MODEL_REQUEST_FAILED"
        assert statuses == [AgentRunStatus.FAILED]
        run_events = _run_lifecycle_records(caplog, result.agent_run_id)
        assert [record.harness_event for record in run_events] == [
            "agent_run_started",
            "agent_run_failed",
        ]
        assert run_events[-1].harness_fields["error_code"] == (
            "MODEL_REQUEST_FAILED"
        )
        assert run_events[-1].harness_fields["duration_ms"] >= 0
        assert [name for name, _value in event_sink.events] == ["started", "failed"]
        assert event_sink.events[0][1].status is AgentRunStatus.RUNNING
        assert event_sink.events[-1][1].status is AgentRunStatus.FAILED
        assert event_sink.failure_messages == [
            "provider request failed before producing a valid response"
        ]

    asyncio.run(scenario())


def test_tool_failure_marks_run_failed_exactly_once(
    agent_storage: AgentStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """将意外执行器失败转换为一次终态转换。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence(
            [AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")])]
        )
        executor = RecordingExecutor(failure=RuntimeError("tool failed"))
        service = _service(agent_storage, model, executor)
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        real_finish = agent_storage.conversations.finish_run
        statuses: list[AgentRunStatus] = []

        def count_finish(
            agent_run_id: UUID,
            status: AgentRunStatus,
            error_code: str | None,
        ) -> AgentRun:
            """委托真实仓库执行时记录终态转换。"""

            statuses.append(status)
            return real_finish(agent_run_id, status, error_code)

        monkeypatch.setattr(agent_storage.conversations, "finish_run", count_finish)

        event_sink = RecordingTurnSink()
        result = await _run_turn(
            agent_storage,
            service,
            turn,
            "key",
            asyncio.Event(),
            event_sink,
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error_code == "SIDECAR_RUNTIME_FAILED"
        assert statuses == [AgentRunStatus.FAILED]
        assert event_sink.failure_messages == [
            "the Agent turn failed because the local runtime raised an unexpected error"
        ]
        assert "tool failed" not in event_sink.failure_messages[0]

    asyncio.run(scenario())


def test_success_publishes_started_text_and_completed_after_durable_finish(
    agent_storage: AgentStorage,
) -> None:
    """在持久化 RUNNING 与 COMPLETED 快照之间暴露精确可见文本。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = _service(
            agent_storage,
            FakeModelSequence([AIMessage(content=" hello\nworld ")]),
            RecordingExecutor(),
        )
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        event_sink = RecordingTurnSink()

        result = await _run_turn(
            agent_storage,
            service,
            turn,
            "key",
            asyncio.Event(),
            event_sink,
        )

        assert result.status is AgentRunStatus.COMPLETED
        assert result.final_text == " hello\nworld "
        assert event_sink.streamed_text == result.final_text
        assert [name for name, _value in event_sink.events] == [
            "started",
            "delta",
            "completed",
        ]
        assert event_sink.events[0][1].status is AgentRunStatus.RUNNING
        assert event_sink.events[-1][1].status is AgentRunStatus.COMPLETED
        assert agent_storage.conversations.get_run(result.agent_run_id).status is (
            AgentRunStatus.COMPLETED
        )

    asyncio.run(scenario())


def test_same_conversation_turns_are_serialized(
    agent_storage: AgentStorage,
) -> None:
    """首轮仍活动时阻止第二轮加载历史。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        conversation_id = agent_storage.conversations.create_conversation()
        blocker = asyncio.Event()
        model = FakeModelSequence(
            [blocker, AIMessage(content="second completed")]
        )
        service = _service(agent_storage, model, RecordingExecutor())
        first = make_turn_input(conversation_id=conversation_id).model_copy(
            update={"api_config_id": config.api_config_id, "user_message": "first"}
        )
        second = make_turn_input(conversation_id=conversation_id).model_copy(
            update={"api_config_id": config.api_config_id, "user_message": "second"}
        )

        first_task = asyncio.create_task(
            _run_turn(agent_storage, service, first, "key-1", asyncio.Event())
        )
        while model.calls == 0:
            await asyncio.sleep(0)
        second_task = asyncio.create_task(
            _run_turn(agent_storage, service, second, "key-2", asyncio.Event())
        )
        await asyncio.sleep(0)
        assert model.calls == 1
        blocker.set()

        first_result, second_result = await asyncio.gather(first_task, second_task)

        assert first_result.status is AgentRunStatus.COMPLETED
        assert second_result.status is AgentRunStatus.COMPLETED
        history = agent_storage.conversations.load_messages(conversation_id)
        assert [message.content for message in history] == [
            "first",
            "released",
            "second",
            "second completed",
        ]

    asyncio.run(scenario())


def test_cancellation_is_returned_as_cancelled_run(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """将用户取消映射为持久化 CANCELLED 结果，不带最终文本。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        blocker = asyncio.Event()
        model = FakeModelSequence([blocker])
        service = _service(agent_storage, model, RecordingExecutor())
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        cancelled = asyncio.Event()
        caplog.set_level(logging.INFO, logger="harness_shell_sidecar.agent.service")

        task = asyncio.create_task(
            _run_turn(agent_storage, service, turn, "key", cancelled)
        )
        while model.calls == 0:
            await asyncio.sleep(0)
        cancelled.set()
        result = await task

        assert result.status is AgentRunStatus.CANCELLED
        assert result.error_code == "AGENT_CANCELLED"
        assert result.final_text is None
        run_events = _run_lifecycle_records(caplog, result.agent_run_id)
        assert [record.harness_event for record in run_events] == [
            "agent_run_started",
            "agent_run_cancelled",
        ]
        assert run_events[-1].harness_fields["error_code"] == "AGENT_CANCELLED"
        assert run_events[-1].harness_fields["duration_ms"] >= 0

    asyncio.run(scenario())


def test_outer_task_cancellation_marks_run_cancelled(
    agent_storage: AgentStorage,
) -> None:
    """dispatcher 取消服务任务时持久化 Run 终态。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = CancellationAwareModel()
        service = _service(agent_storage, model, RecordingExecutor())
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        task = asyncio.create_task(
            _run_turn(agent_storage, service, turn, "key", asyncio.Event())
        )
        await model.started.wait()

        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            row = agent_storage.database.execute(
                "SELECT status, error_code FROM agent_runs"
            ).fetchone()
            assert row == ("CANCELLED", "AGENT_CANCELLED")
        finally:
            model.release.set()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_outer_task_cancellation_during_tool_marks_run_cancelled(
    agent_storage: AgentStorage,
) -> None:
    """dispatcher 关闭时持久化取消并等待工具清理。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence(
            [AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")])]
        )
        executor = BlockingExecutor()
        service = _service(agent_storage, model, executor)
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        task = asyncio.create_task(
            _run_turn(agent_storage, service, turn, "key", asyncio.Event())
        )
        await executor.started.wait()

        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert executor.stopped.is_set()
            row = agent_storage.database.execute(
                "SELECT status, error_code FROM agent_runs"
            ).fetchone()
            assert row == ("CANCELLED", "AGENT_CANCELLED")
        finally:
            executor.release.set()

    asyncio.run(scenario())


def test_missing_or_disabled_api_config_fails_before_run_creation(
    agent_storage: AgentStorage,
) -> None:
    """创建持久化 Agent Run 前拒绝不可用 Provider 配置。"""

    async def scenario() -> None:
        service = _service(
            agent_storage,
            FakeModelSequence(),
            RecordingExecutor(),
        )
        missing = make_turn_input().model_copy(update={"api_config_id": uuid4()})

        with pytest.raises(AgentServiceError) as missing_error:
            await service.run_turn(
                missing,
                SecretStr("key"),
                asyncio.Event(),
                expected_config=None,
                event_sink=RecordingTurnSink(),
            )
        assert missing_error.value.error_code == "MODEL_API_CONFIG_NOT_FOUND"

        disabled_config = agent_storage.api_configs.create(
            valid_api_config_input().model_copy(update={"enabled": False})
        )
        disabled = make_turn_input().model_copy(
            update={"api_config_id": disabled_config.api_config_id}
        )
        with pytest.raises(AgentServiceError) as disabled_error:
            await service.run_turn(
                disabled,
                SecretStr("key"),
                asyncio.Event(),
                expected_config=disabled_config,
                event_sink=RecordingTurnSink(),
            )
        assert disabled_error.value.error_code == "MODEL_API_CONFIG_DISABLED"
        assert agent_storage.database.execute(
            "SELECT COUNT(*) FROM agent_runs"
        ).fetchone() == (0,)

    asyncio.run(scenario())


def test_missing_session_fails_before_conversation_run_or_model_call(
    agent_storage: AgentStorage,
) -> None:
    """任何持久化轮次工作前要求权威已连接 Session。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence([AIMessage(content="must not run")])
        service = _service(
            agent_storage,
            model,
            RecordingExecutor(),
            session_is_available=lambda _session_id: False,
        )
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )

        with pytest.raises(AgentServiceError) as error:
            await service.run_turn(
                turn,
                SecretStr("key"),
                asyncio.Event(),
                expected_config=config,
                event_sink=RecordingTurnSink(),
            )

        assert error.value.error_code == "SSH_SESSION_UNAVAILABLE"
        assert model.calls == 0
        assert agent_storage.database.execute(
            "SELECT COUNT(*) FROM agent_conversations"
        ).fetchone() == (0,)
        assert agent_storage.database.execute(
            "SELECT COUNT(*) FROM agent_runs"
        ).fetchone() == (0,)

    asyncio.run(scenario())


def test_queued_turn_rejects_config_change_before_starting_second_run(
    agent_storage: AgentStorage,
) -> None:
    """取得会话锁后再次比较完整 handler 快照。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        conversation_id = agent_storage.conversations.create_conversation()
        blocker = asyncio.Event()
        model = FakeModelSequence([blocker, AIMessage(content="must not run")])
        service = _service(agent_storage, model, RecordingExecutor())
        first = make_turn_input(conversation_id=conversation_id).model_copy(
            update={"api_config_id": config.api_config_id, "user_message": "first"}
        )
        second = make_turn_input(conversation_id=conversation_id).model_copy(
            update={"api_config_id": config.api_config_id, "user_message": "second"}
        )

        first_task = asyncio.create_task(
            service.run_turn(
                first,
                SecretStr("key-1"),
                asyncio.Event(),
                expected_config=config,
                event_sink=RecordingTurnSink(),
            )
        )
        while model.calls == 0:
            await asyncio.sleep(0)
        second_task = asyncio.create_task(
            service.run_turn(
                second,
                SecretStr("key-2"),
                asyncio.Event(),
                expected_config=config,
                event_sink=RecordingTurnSink(),
            )
        )
        await asyncio.sleep(0)
        agent_storage.api_configs.update(
            config.api_config_id,
            valid_api_config_input().model_copy(update={"model": "changed-model"}),
        )
        blocker.set()

        first_result = await first_task
        with pytest.raises(AgentServiceError) as error:
            await second_task

        assert first_result.status is AgentRunStatus.COMPLETED
        assert error.value.error_code == "MODEL_API_CONFIG_CHANGED"
        assert model.calls == 1
        assert agent_storage.database.execute(
            "SELECT COUNT(*) FROM agent_runs"
        ).fetchone() == (1,)
        assert service._conversation_locks == {}

    asyncio.run(scenario())


def test_successful_turn_removes_unused_conversation_lock(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """避免为每个历史会话永久保留锁条目。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = _service(
            agent_storage,
            FakeModelSequence([AIMessage(content="done")]),
            RecordingExecutor(),
        )
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        caplog.set_level(logging.INFO, logger="harness_shell_sidecar.agent.service")

        result = await service.run_turn(
            turn,
            SecretStr("key"),
            asyncio.Event(),
            expected_config=config,
            event_sink=RecordingTurnSink(),
        )

        assert service._conversation_locks == {}
        run_events = _run_lifecycle_records(caplog, result.agent_run_id)
        assert [record.harness_event for record in run_events] == [
            "agent_run_started",
            "agent_run_completed",
        ]
        assert "error_code" not in run_events[-1].harness_fields
        assert run_events[-1].harness_fields["duration_ms"] >= 0

    asyncio.run(scenario())


def test_react_limit_logs_one_failed_terminal_lifecycle(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """将 LIMIT_REACHED 作为唯一失败终态生命周期事件。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence(
            [
                AIMessage(
                    content="",
                    tool_calls=[make_tool_call(f"call-{index}", "pwd")],
                )
                for index in range(1, 130)
            ]
        )
        service = _service(agent_storage, model, RecordingExecutor())
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )
        caplog.set_level(logging.INFO, logger="harness_shell_sidecar.agent.service")

        result = await _run_turn(agent_storage, service, turn, "key", asyncio.Event())

        assert result.status is AgentRunStatus.LIMIT_REACHED
        run_events = _run_lifecycle_records(caplog, result.agent_run_id)
        assert [record.harness_event for record in run_events] == [
            "agent_run_started",
            "agent_run_failed",
        ]
        assert run_events[-1].harness_fields["error_code"] == (
            "REACT_LIMIT_REACHED"
        )
        assert run_events[-1].harness_fields["duration_ms"] >= 0

    asyncio.run(scenario())


def test_oversized_final_response_marks_run_failed_before_returning_error(
    agent_storage: AgentStorage,
) -> None:
    """保持持久化 Run 终态与传输拒绝一致。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        model = FakeModelSequence(
            [AIMessage(content="x" * (MAX_JSON_BODY_BYTES + 1))]
        )
        service = _service(agent_storage, model, RecordingExecutor())
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )

        event_sink = RecordingTurnSink()
        result = await service.run_turn(
            turn,
            SecretStr("key"),
            asyncio.Event(),
            expected_config=config,
            event_sink=event_sink,
        )

        assert result.error_code == "AGENT_RESPONSE_TOO_LARGE"
        assert [name for name, _value in event_sink.events][-1] == "failed"
        row = agent_storage.database.execute(
            "SELECT status, error_code FROM agent_runs"
        ).fetchone()
        assert row == ("FAILED", "AGENT_RESPONSE_TOO_LARGE")

    asyncio.run(scenario())


def test_response_budget_uses_final_react_iteration(
    agent_storage: AgentStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拒绝恰好容纳第零次却无法容纳第 128 次迭代的边界。"""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        conversation_id = uuid4()
        run_id = uuid4()
        final_text = "done"
        zero_iteration = AgentTurnResult(
            conversation_id=conversation_id,
            agent_run_id=run_id,
            status=AgentRunStatus.COMPLETED,
            final_text=final_text,
            react_iteration=0,
            error_code=None,
        )
        candidate = {
            "request_id": str(UUID(int=0)),
            **zero_iteration.model_dump(mode="json"),
        }
        zero_size = len(
            json.dumps(
                candidate,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        monkeypatch.setattr(agent_service_module, "MAX_JSON_BODY_BYTES", zero_size)

        outputs = [
            AIMessage(
                content="",
                tool_calls=[make_tool_call(f"call-{index}", "pwd")],
            )
            for index in range(128)
        ]
        outputs.append(AIMessage(content=final_text))
        service = _service(
            agent_storage,
            FakeModelSequence(outputs),
            RecordingExecutor(),
        )
        turn = make_turn_input().model_copy(
            update={"api_config_id": config.api_config_id}
        )

        event_sink = RecordingTurnSink()
        result = await service.run_turn(
            turn,
            SecretStr("key"),
            asyncio.Event(),
            expected_config=config,
            event_sink=event_sink,
        )

        assert result.error_code == "AGENT_RESPONSE_TOO_LARGE"
        assert [name for name, _value in event_sink.events][-1] == "failed"
        row = agent_storage.database.execute(
            "SELECT status, react_iteration, error_code FROM agent_runs"
        ).fetchone()
        assert row == ("FAILED", 128, "AGENT_RESPONSE_TOO_LARGE")

    asyncio.run(scenario())
