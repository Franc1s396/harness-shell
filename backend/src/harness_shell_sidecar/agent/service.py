"""顶层 Agent 轮次生命周期与按会话串行执行。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage
from pydantic import SecretStr

from harness_shell_sidecar.runtime.models import MAX_JSON_BODY_BYTES
from .api_configs import ApiConfigRepository
from .context import ContextService
from .context_budget import ContextBudget
from .contracts import (
    AgentRun,
    AgentRunStatus,
    AgentTurnInput,
    AgentTurnResult,
    ModelApiConfig,
)
from .conversations import ConversationRepository
from harness_shell_sidecar.storage import RuntimeDatabase, PlaintextRecordStore
from .executor import AgentCancelled
from .graph import (
    AgentGraphContext,
    AgentGraphDependencies,
    AgentGraphState,
    CommandExecutor,
    ModelInvoker,
    build_agent_graph,
)
from .streaming import AgentTurnEventSink
from .tools import CommandSafetyReviewer


_PUBLIC_RUN_FAILURE_CODES = frozenset(
    {
        "CONTEXT_TOKENIZER_UNAVAILABLE",
        "CONTEXT_BUDGET_EXCEEDED",
        "CONTEXT_COMPACTION_FAILED",
        "CONTEXT_SUMMARY_INVALID",
        "AGENT_CANCELLED",
        "AGENT_RESPONSE_TOO_LARGE",
        "MODEL_NETWORK_TIMEOUT",
        "MODEL_REQUEST_FAILED",
        "MODEL_RESPONSE_INVALID",
        "REACT_LIMIT_REACHED",
        "SIDECAR_RUNTIME_FAILED",
        "SSH_SESSION_UNAVAILABLE",
    }
)
_UNEXPECTED_RUN_FAILURE_MESSAGE = (
    "the Agent turn failed because the local runtime raised an unexpected error"
)
_REACT_LIMIT_FAILURE_MESSAGE = "the Agent reached the ReAct iteration limit"
LOGGER = logging.getLogger("harness_shell_sidecar.agent.service")


class AgentServiceError(RuntimeError):
    """暴露持久化 Run 启动前发生的稳定失败。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存公开错误码与已审查的非敏感失败原因。"""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # 提供给 handler 的稳定失败错误码。
        self.safe_message = message  # 经过审查且不含秘密材料的详情。


@dataclass(slots=True)
class _ConversationLockEntry:
    """跟踪一个会话锁及其所有持有者和等待者。"""

    lock: asyncio.Lock
    users: int = 0


class AgentService:
    """校验轮次、串行化所属会话，并负责图生命周期映射。"""

    def __init__(
        self,
        database: RuntimeDatabase,
        executor: CommandExecutor,
        gateway: ModelInvoker,
        context: ContextService,
        session_is_available: Callable[[UUID], bool],
        *, budget: ContextBudget | None = None,
    ) -> None:
        """利用长期非秘密运行时协作者构建可复用图。"""

        self._database = database  # 短数据库操作的工厂，绝不保存跨 await Session。
        self._session_is_available = session_is_available
        self._conversation_locks: dict[UUID, _ConversationLockEntry] = {}
        dependencies = AgentGraphDependencies(
            database=database,
            context=context,
            gateway=gateway,
            reviewer=CommandSafetyReviewer(),
            executor=executor,
            budget=budget,
        )
        self._graph = build_agent_graph(dependencies)  # 编译时不使用 checkpointer。

    async def run_turn(
        self,
        request: AgentTurnInput,
        api_key: SecretStr,
        cancelled: asyncio.Event,
        *,
        expected_config: ModelApiConfig | None,
        event_sink: AgentTurnEventSink,
    ) -> AgentTurnResult:
        """执行一轮，并围绕持久化 Run 状态发布生命周期事件。"""

        # 1. 创建持久化状态前先验证取消、Provider 配置与活动 SSH 会话。
        self._validate_run_authorities(request, expected_config, cancelled)

        conversation_id = request.conversation_id
        with self._database.write_session() as session:
            repository = ConversationRepository(session, PlaintextRecordStore(session))
            if conversation_id is None:
                conversation_id = repository.create_conversation()
            elif not repository.conversation_exists(conversation_id):
                raise AgentServiceError(
                    "AGENT_CONVERSATION_NOT_FOUND",
                    "the requested conversation does not exist",
                )

        # 2. 按会话串行执行；取得锁后再次复核权威状态，再创建 RUNNING Run。
        async with self._conversation_lock(conversation_id):
            config = self._validate_run_authorities(
                request,
                expected_config,
                cancelled,
            )
            started_ns = time.monotonic_ns()
            with self._database.write_session() as session:
                run = ConversationRepository(session, PlaintextRecordStore(session)).start_run(
                    conversation_id,
                    request.ssh_session_id,
                    request.api_config_id,
                )
            LOGGER.info(
                "agent_run_started agent_run_id=%s conversation_id=%s "
                "ssh_session_id=%s api_config_id=%s api_type=%s model=%s "
                "react_iteration=%s",
                run.agent_run_id,
                conversation_id,
                request.ssh_session_id,
                config.api_config_id,
                config.api_type.value,
                config.model,
                run.react_iteration,
                extra={
                    "harness_event": "agent_run_started",
                    "harness_fields": {
                        "agent_run_id": str(run.agent_run_id),
                        "conversation_id": str(conversation_id),
                        "ssh_session_id": str(request.ssh_session_id),
                        "api_config_id": str(config.api_config_id),
                        "api_type": config.api_type.value,
                        "model": config.model,
                        "react_iteration": run.react_iteration,
                    },
                },
            )
            # 3. 分开构造图状态与短生命周期调用上下文，秘密不进入图状态。
            initial_state: AgentGraphState = {
                "agent_run_id": run.agent_run_id,
                "conversation_id": conversation_id,
                "ssh_session_id": request.ssh_session_id,
                "api_config_id": request.api_config_id,
                "messages": [],
                "model_messages": [],
                "records": [],
                "summary": None,
                "react_iteration": 0,
                "run_status": AgentRunStatus.RUNNING,
                "last_error_code": None,
            }
            graph_context = AgentGraphContext(
                api_config=config,
                api_key=api_key,
                cancelled=cancelled,
                user_message=request.user_message,
                text_sink=event_sink,
            )
            try:
                # 4. Run 已持久化后发布 started，再运行图和最终文本一致性检查。
                await event_sink.started(run)
                state = await self._graph.ainvoke(
                    initial_state,
                    config={"recursion_limit": 1024},
                    context=graph_context,
                )
                if state["run_status"] is AgentRunStatus.COMPLETED:
                    final_text = _final_text(state)
                    if event_sink.streamed_text != final_text:
                        raise AgentServiceError(
                            "MODEL_RESPONSE_INVALID",
                            "streamed visible text did not match the final model response",
                        )
                    _require_agent_result_fits(
                        run,
                        final_text,
                        react_iteration=state["react_iteration"],
                    )
                    with self._database.write_session() as session:
                        finished = ConversationRepository(session, PlaintextRecordStore(session)).finish_run(
                            run.agent_run_id,
                            AgentRunStatus.COMPLETED,
                            None,
                        )
                    _log_terminal_run(finished, config, started_ns)
                    await event_sink.completed(finished)
                    return _result_from_run(finished, final_text=final_text)
            # 5. 异常和取消路径先持久化终态；协程取消继续向外传播。
            except asyncio.CancelledError:
                # 即使 dispatcher 正在关闭，也必须持久化 Run 终态。
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.CANCELLED,
                    "AGENT_CANCELLED",
                )
                _log_terminal_run(finished, config, started_ns)
                raise
            except AgentCancelled as error:
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.CANCELLED,
                    error.error_code,
                )
                _log_terminal_run(finished, config, started_ns)
                await event_sink.failed(finished, error.safe_message)
                return _result_from_run(finished, final_text=None)
            except AgentServiceError as error:
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.FAILED,
                    error.error_code,
                )
                _log_terminal_run(finished, config, started_ns)
                await event_sink.failed(finished, error.safe_message)
                return _result_from_run(finished, final_text=None)
            except Exception as error:
                error_code = getattr(error, "error_code", "SIDECAR_RUNTIME_FAILED")
                if error_code not in _PUBLIC_RUN_FAILURE_CODES:
                    error_code = "SIDECAR_RUNTIME_FAILED"
                    failure_message = _UNEXPECTED_RUN_FAILURE_MESSAGE
                else:
                    candidate = getattr(error, "safe_message", None)
                    failure_message = (
                        candidate
                        if isinstance(candidate, str)
                        else _UNEXPECTED_RUN_FAILURE_MESSAGE
                    )
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.FAILED,
                    error_code,
                )
                _log_terminal_run(finished, config, started_ns)
                await event_sink.failed(finished, failure_message)
                return _result_from_run(finished, final_text=None)

            # 6. 图自行进入终态时重新读取权威记录，再发布匹配的终止事件。
            with self._database.read_session() as session:
                finished = ConversationRepository(session, PlaintextRecordStore(session)).get_run(run.agent_run_id)
            if finished is None or finished.status is AgentRunStatus.RUNNING:
                raise RuntimeError("Agent graph returned without a durable terminal Run")
            _log_terminal_run(finished, config, started_ns)
            final_text = _final_text(state) if finished.status is AgentRunStatus.COMPLETED else None
            if finished.status is AgentRunStatus.COMPLETED:
                if event_sink.streamed_text != final_text:
                    raise RuntimeError("durable final text does not match streamed text")
                await event_sink.completed(finished)
            else:
                if finished.status is not AgentRunStatus.LIMIT_REACHED:
                    raise RuntimeError(
                        "Agent graph returned an unsupported terminal failure status"
                    )
                await event_sink.failed(finished, _REACT_LIMIT_FAILURE_MESSAGE)
            return _result_from_run(finished, final_text=final_text)

    def _validate_run_authorities(
        self,
        request: AgentTurnInput,
        expected_config: ModelApiConfig | None,
        cancelled: asyncio.Event,
    ) -> ModelApiConfig:
        """重新检查取消、完整配置快照和活动 Session 权威状态。"""

        if cancelled.is_set():
            raise AgentServiceError(
                "AGENT_CANCELLED",
                "the turn was cancelled before its run authorities were validated",
            )
        with self._database.read_session() as session:
            config = ApiConfigRepository(session).get(request.api_config_id)
        if config is None:
            raise AgentServiceError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "the selected model API configuration does not exist",
            )
        if not config.enabled:
            raise AgentServiceError(
                "MODEL_API_CONFIG_DISABLED",
                "the selected model API configuration is disabled",
            )
        if expected_config is None or config != expected_config:
            raise AgentServiceError(
                "MODEL_API_CONFIG_CHANGED",
                "the selected model API configuration changed before execution",
            )
        if not self._session_is_available(request.ssh_session_id):
            raise AgentServiceError(
                "SSH_SESSION_UNAVAILABLE",
                "the SSH session frozen for this turn is not available",
            )
        return config

    @asynccontextmanager
    async def _conversation_lock(
        self,
        conversation_id: UUID,
    ) -> AsyncIterator[None]:
        """串行化同一会话，最后一个使用者退出后移除锁。"""

        entry = self._conversation_locks.get(conversation_id)
        if entry is None:
            entry = _ConversationLockEntry(lock=asyncio.Lock())
            self._conversation_locks[conversation_id] = entry
        entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.users -= 1
            if entry.users == 0:
                current = self._conversation_locks.get(conversation_id)
                if current is entry:
                    del self._conversation_locks[conversation_id]

    def _finish_if_running(
        self,
        original_run: AgentRun,
        status: AgentRunStatus,
        error_code: str,
    ) -> AgentRun:
        """除非图节点已进入终态，否则执行一次失败转换。"""

        with self._database.write_session() as session:
            repository = ConversationRepository(session, PlaintextRecordStore(session))
            current = repository.get_run(original_run.agent_run_id)
            if current is None:
                raise RuntimeError("Agent run disappeared during terminal failure mapping")
            if current.status is not AgentRunStatus.RUNNING:
                return current
            return repository.finish_run(
                original_run.agent_run_id,
                status,
                error_code,
            )


def _log_terminal_run(
    run: AgentRun,
    config: ModelApiConfig,
    started_ns: int,
) -> None:
    """为已知的持久化终态 Run 发出且仅发出一次生命周期事件。"""

    event = {
        AgentRunStatus.COMPLETED: "agent_run_completed",
        AgentRunStatus.CANCELLED: "agent_run_cancelled",
        AgentRunStatus.FAILED: "agent_run_failed",
        AgentRunStatus.LIMIT_REACHED: "agent_run_failed",
    }[run.status]
    message = (
        "%s agent_run_id=%s conversation_id=%s ssh_session_id=%s "
        "api_config_id=%s api_type=%s model=%s react_iteration=%s "
        "duration_ms=%s"
    )
    arguments = (
        event,
        run.agent_run_id,
        run.conversation_id,
        run.ssh_session_id,
        run.api_config_id,
        config.api_type.value,
        config.model,
        run.react_iteration,
        (time.monotonic_ns() - started_ns) // 1_000_000,
    )
    fields = {
        "agent_run_id": str(run.agent_run_id),
        "conversation_id": str(run.conversation_id),
        "ssh_session_id": str(run.ssh_session_id),
        "api_config_id": str(run.api_config_id),
        "api_type": config.api_type.value,
        "model": config.model,
        "react_iteration": run.react_iteration,
        "duration_ms": arguments[-1],
    }
    if run.status in {AgentRunStatus.FAILED, AgentRunStatus.LIMIT_REACHED}:
        LOGGER.error(
            f"{message} error_code=%s",
            *arguments,
            run.error_code,
            extra={
                "harness_event": event,
                "harness_fields": {**fields, "error_code": run.error_code},
            },
        )
    elif run.error_code is not None:
        LOGGER.info(
            f"{message} error_code=%s",
            *arguments,
            run.error_code,
            extra={
                "harness_event": event,
                "harness_fields": {**fields, "error_code": run.error_code},
            },
        )
    else:
        LOGGER.info(
            message,
            *arguments,
            extra={
                "harness_event": event,
                "harness_fields": fields,
            },
        )


def _final_text(state: dict[str, Any]) -> str:
    """从字符串或标准 Responses 内容块提取最终文本。"""

    messages = state.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RuntimeError("completed Agent state does not contain messages")
    message = messages[-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        raise RuntimeError("completed Agent state does not end in final AI text")
    return str(message.text)


def _result_from_run(run: AgentRun, *, final_text: str | None) -> AgentTurnResult:
    """将持久化 Run 快照投影为有界内部结果。"""

    return AgentTurnResult(
        conversation_id=run.conversation_id,
        agent_run_id=run.agent_run_id,
        status=run.status,
        final_text=final_text,
        react_iteration=run.react_iteration,
        error_code=run.error_code,
    )


def _require_agent_result_fits(
    run: AgentRun,
    final_text: str,
    *,
    react_iteration: int,
) -> None:
    """保持引入 SSE 前完整结果的逻辑字节预算不变。"""

    result = AgentTurnResult(
        conversation_id=run.conversation_id,
        agent_run_id=run.agent_run_id,
        status=AgentRunStatus.COMPLETED,
        final_text=final_text,
        react_iteration=react_iteration,
        error_code=None,
    )
    candidate = {
        "request_id": str(UUID(int=0)),
        **result.model_dump(mode="json"),
    }
    encoded_size = len(
        json.dumps(
            candidate,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    if encoded_size > MAX_JSON_BODY_BYTES:
        raise AgentServiceError(
            "AGENT_RESPONSE_TOO_LARGE",
            "the serialized Agent result exceeded the HTTP response limit",
        )
