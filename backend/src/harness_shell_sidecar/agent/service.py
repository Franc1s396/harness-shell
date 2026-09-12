"""顶层 Agent 轮次生命周期与按会话串行执行。"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage
from pydantic import SecretStr
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from .approval_models import ApprovalRequest, ApprovalTarget, ApprovalDecision, ApprovalResolution
from .approvals import ApprovalRegistry, ApprovalError

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
from .conversations import ConversationRepository, ConversationRepositoryError
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
        *, ssh_sessions: SshSessionRegistry, budget: ContextBudget | None = None,
    ) -> None:
        """保存长期协作者，各 Run 独立构建内存图。"""

        self._database = database  # 短数据库操作的工厂，绝不保存跨 await Session。
        self._session_is_available = session_is_available
        self._conversation_locks: dict[UUID | tuple[str, UUID], _ConversationLockEntry] = {}  # 分域串行化消息尝试和会话。
        dependencies = AgentGraphDependencies(
            database=database,
            context=context,
            gateway=gateway,
            reviewer=CommandSafetyReviewer(),
            executor=executor,
            budget=budget,
        )
        self._dependencies = dependencies  # 各 Run 借用依赖，独立编译内存图。
        self._ssh_sessions = ssh_sessions  # 借用 Runtime 的唯一会话权威。
        self._approvals = ApprovalRegistry()  # 只持有当前 Run 决定，结束时释放。

    async def run_turn(
        self, request: AgentTurnInput, api_key: SecretStr, cancelled: asyncio.Event,
        *, expected_config: ModelApiConfig | None, event_sink: AgentTurnEventSink,
    ) -> AgentTurnResult:
        """先锁定用户消息身份，再进入会话锁，避免首帧丢失时并发创建会话。"""
        # 1. retry 才是调用方的重试意图；重试必须提供用于定位原轮次的消息 ID。
        # 消息 ID 是客户端输入的关联键，不是授权凭证；实际目标仍需按数据库校验。
        if request.retry and request.user_message_id is None:
            raise AgentServiceError("AGENT_RETRY_CONFLICT", "retry requires a user message identity")
        try:
            # 2. 此分支只决定是否加消息锁，不据此判断普通发送还是重试。
            # 无 ID 的普通发送仍被接口允许；当前 WebView 的普通发送会提供新 ID。
            if request.user_message_id is None:
                return await self._run_turn(request, api_key, cancelled,
                    expected_config=expected_config, event_sink=event_sink)
            # 3. 首次发送和重试共用此锁，避免同一消息在尚未获知会话 ID 时并发执行。
            # _run_turn 在持锁期间检查 retry 和已有 Run，再取得具体会话锁。
            async with self._conversation_lock(("message", request.user_message_id)):
                return await self._run_turn(request, api_key, cancelled,
                    expected_config=expected_config, event_sink=event_sink)
        except ConversationRepositoryError as error:
            raise AgentServiceError(error.error_code, error.safe_message) from error

    async def _run_turn(
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
            # 查到已有 Run 不代表自动重试：普通发送复用已有 ID 必须拒绝。
            # retry=true 但没有已落库 Run 时，没有旧历史可替换，继续创建新 Run。
            previous = repository.find_user_turn(request.user_message_id) if request.user_message_id else None
            if previous is not None:
                # 只有显式重试且会话身份不冲突，才能使用数据库中的原会话。
                # conversation_id 允许为空，以覆盖客户端没有收到 started 的情况。
                if not request.retry or (conversation_id is not None and conversation_id != previous.conversation_id):
                    raise AgentServiceError("AGENT_RETRY_CONFLICT", "user message already belongs to an existing turn")
                conversation_id = previous.conversation_id
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
                repository = ConversationRepository(session, PlaintextRecordStore(session))
                if previous is not None:
                    # 前面的校验已确保 retry=true；这里再检查末轮终态和已保存的用户原文。
                    # 清理旧消息和创建新 Run 共用事务，任何失败都整体回滚。
                    repository.remove_last_turn(previous, request.user_message)
                run = repository.start_run(
                    conversation_id,
                    request.ssh_session_id,
                    request.api_config_id,
                    user_message_id=request.user_message_id,
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
                "summaries": (),
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
                approval_registry=self._approvals,
            )
            try:
                # 4. Run 已持久化后发布 started，再运行图和最终文本一致性检查。
                await event_sink.started(run)
                state = await self._invoke_until_terminal(initial_state, graph_context)
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

    def decide_approval(self, approval_id: UUID, decision: ApprovalDecision) -> ApprovalResolution:
        """HTTP 只提交决定，原 worker 独占恢复和执行。"""
        resolution = self._approvals.decide(approval_id, decision)
        if not self._session_is_available(decision.ssh_session_id):
            self._approvals.invalidate_run(decision.agent_run_id, "session_unavailable")
            raise ApprovalError("AGENT_APPROVAL_INACTIVE", "the bound SSH session is unavailable")
        return resolution

    async def _invoke_until_terminal(self, initial_state: AgentGraphState, context: AgentGraphContext) -> AgentGraphState:
        """原 SSE worker 驱动内存图直到真正结束，并确定性回收 checkpoint。"""
        from dataclasses import replace
        run_id = initial_state["agent_run_id"]
        session = self._ssh_sessions.get(initial_state["ssh_session_id"])
        if session is None:
            raise AgentServiceError("SSH_SESSION_UNAVAILABLE", "the bound SSH session is unavailable")
        context = replace(context, approval_target=ApprovalTarget(
            display_name=session.host_label, host=session.host, port=session.port, username=session.username))
        # 只允许图中实际保存的项目类型，秘密和运行时资源不进入 serializer。
        saver = InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=[
            ("harness_shell_sidecar.agent.context_models", "ContextMessage"),
            ("harness_shell_sidecar.agent.context_models", "ContextSummary"),
            ("harness_shell_sidecar.agent.contracts", "AgentRunStatus"),
        ]))
        graph = build_agent_graph(self._dependencies, checkpointer=saver)
        config = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 2048}
        graph_input = initial_state
        try:
            while True:
                if context.cancelled.is_set():
                    raise AgentCancelled()
                state = await graph.ainvoke(graph_input, config=config, context=context)
                interrupts = state.get("__interrupt__", ())
                if not interrupts:
                    return state
                if len(interrupts) != 1:
                    raise AgentServiceError("AGENT_APPROVAL_CONFLICT", "exactly one approval interrupt is required")
                item = interrupts[0]
                request = ApprovalRequest.model_validate_json(json.dumps(item.value))
                if (request.agent_run_id != run_id or request.conversation_id != initial_state["conversation_id"]
                        or request.ssh_session_id != initial_state["ssh_session_id"]):
                    raise AgentServiceError("AGENT_APPROVAL_CONFLICT", "approval interrupt identity does not match the run")
                self._approvals.register(request)
                await context.text_sink.approval_requested(request)
                resolution = await self._wait_for_approval(request, context.cancelled)
                await context.text_sink.approval_resolved(resolution, request.tool_call_id)
                graph_input = Command(resume={item.id: {"approval_id": str(request.approval_id),
                    "decision": "approve" if resolution.status == "APPROVED" else "reject"}})
        finally:
            # 内存 saver 的 delete 没有网络 I/O；同步删除确保取消 scope 内也能完成清理。
            primary_failure = sys.exception()
            cleanup_failure: Exception | None = None
            try:
                self._approvals.release_run(run_id)
            except Exception as error:
                cleanup_failure = error
                LOGGER.exception("agent_approval_cleanup_failed")
            try:
                saver.delete_thread(str(run_id))
            except Exception as error:
                if cleanup_failure is None:
                    cleanup_failure = error
                LOGGER.exception("agent_checkpoint_cleanup_failed")
            # 清理失败可见，但不能把原业务失败或取消替换成另一个终态。
            if primary_failure is None and cleanup_failure is not None:
                raise cleanup_failure

    async def _wait_for_approval(self, request: ApprovalRequest, cancelled: asyncio.Event) -> ApprovalResolution:
        """明确不限时等待用户，同时监听取消与原 SSH 传输失效。"""
        decision = asyncio.create_task(self._approvals.wait(request.approval_id))
        cancel = asyncio.create_task(cancelled.wait())
        unavailable = asyncio.create_task(self._ssh_sessions.wait_unavailable(request.ssh_session_id))
        tasks = (decision, cancel, unavailable)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if cancelled.is_set():
                self._approvals.invalidate_run(request.agent_run_id, "run_cancelled")
                raise AgentCancelled()
            if unavailable in done or not self._session_is_available(request.ssh_session_id):
                if unavailable in done:
                    unavailable.result()
                self._approvals.invalidate_run(request.agent_run_id, "session_unavailable")
                raise AgentServiceError("SSH_SESSION_UNAVAILABLE", "the SSH session closed while awaiting approval")
            resolution = decision.result()
            if resolution.status == "INVALIDATED":
                raise AgentServiceError("AGENT_APPROVAL_INACTIVE", "the pending approval is no longer active")
            return resolution
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
        conversation_id: UUID | tuple[str, UUID],
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
