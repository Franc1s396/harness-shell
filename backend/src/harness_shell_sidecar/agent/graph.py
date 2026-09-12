"""具有显式持久化与业务限制的自定义 LangGraph ReAct 循环。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypedDict
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt
from .approval_models import ApprovalRequest, ApprovalTarget
from .approvals import ApprovalRegistry
from .command_policy import classify_command
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import SecretStr, ValidationError

from .context import ContextService
from .context_models import ContextMessage, ContextSummary, AgentContextPolicy
from .context_budget import ContextBudget
from .context_summaries import ContextSummaryRepository
from .context_compaction import ContextCompactor, SummaryInvoker
from .tokenizer import load_local_encoding, tokenizer_resource_dir
from .contracts import (
    AgentRunStatus,
    CommandToolEnvelope,
    ExecuteCommandArguments,
    ModelApiConfig,
)
from .conversations import ConversationRepository
from harness_shell_sidecar.storage import RuntimeDatabase, PlaintextRecordStore
from .streaming import AgentTextDeltaSink, AgentTurnEventSink
from .executor import AgentCancelled
from .tools import (
    CommandRejected,
    CommandSafetyReviewer,
    tool_message,
)


LOGGER = logging.getLogger("harness_shell_sidecar.agent.graph")


class AgentGraphState(TypedDict):
    """显式 Agent 图节点之间交换的非持久化状态。"""

    agent_run_id: UUID
    conversation_id: UUID
    ssh_session_id: UUID
    api_config_id: UUID
    messages: Annotated[list[AnyMessage], add_messages]
    model_messages: list[AnyMessage]
    records: list[ContextMessage]  # 有序权威记录；使用整体替换语义。
    summaries: tuple[ContextSummary, ...]  # 全部有序历史摘要，整体替换，不作为 SSE 可见文本。
    react_iteration: int
    run_status: AgentRunStatus
    last_error_code: str | None
    pending_approval: dict[str, object] | None  # JSON 审核描述，不包含运行时资源。
    approval_decision: Literal["approve", "reject"] | None  # 当前工具的恢复决定，结果保存后清空。
    tool_results: list[ToolMessage]  # 当前工具结果，独立节点保存。


@dataclass(frozen=True, slots=True)
class AgentGraphContext:
    """将敏感或本轮局部值保留在图状态和持久化之外。"""

    #: 本 Run 冻结的非秘密 Provider 配置。
    api_config: ModelApiConfig
    #: 仅提供给当前图调用的短生命周期 Provider 秘密。
    api_key: SecretStr
    #: 调用方拥有的取消事件，由模型和 SSH 操作共享。
    cancelled: asyncio.Event
    #: 由 load_context 持久化的当前用户输入。
    user_message: str
    #: 本 Run 局部的文本与工具状态接收端，不进入图状态或持久化。
    text_sink: AgentTurnEventSink
    #: 仅借用 Run 的授权注册表，绝不进入 checkpoint。
    approval_registry: ApprovalRegistry | None = None
    #: 已建立 SSH 会话的冻结显示快照。
    approval_target: ApprovalTarget | None = None


class ModelInvoker(SummaryInvoker, Protocol):
    """描述图节点所需的模型网关接口。"""

    async def invoke(
        self,
        config: ModelApiConfig,
        api_key: SecretStr,
        messages: Sequence[AnyMessage],
        cancelled: asyncio.Event,
        text_sink: AgentTextDeltaSink,
    ) -> AIMessage:
        """返回完整模型消息，同时流式发布最终可见文本。"""


class CommandExecutor(Protocol):
    """描述工具节点所需的绑定 SSH 执行器接口。"""

    async def execute(
        self,
        ssh_session_id: UUID,
        command: str,
        cancelled: asyncio.Event,
    ) -> CommandToolEnvelope:
        """执行已审查命令并返回稳定结果信封。"""


@dataclass(frozen=True, slots=True)
class AgentGraphDependencies:
    """集中保存编译后图捕获的长期非秘密协作者。"""

    #: 完整权威历史和 Run 生命周期的管理者。
    database: RuntimeDatabase
    #: 负责中断历史修复与模型窗口投影。
    context: ContextService
    #: 双 API 模型调用网关。
    gateway: ModelInvoker
    #: 使用固定正则表达式的命令审查器。
    reviewer: CommandSafetyReviewer
    #: 在冻结会话上执行非 PTY SSH 命令的执行器。
    executor: CommandExecutor
    #: 可选的显式运行时估算器；测试可注入受控预算。
    budget: ContextBudget | None = None


NodePatch = dict[str, object]
NodeHandler = Callable[
    [AgentGraphState, Runtime[AgentGraphContext]],
    NodePatch | Awaitable[NodePatch],
]


def _instrument_agent_node(node: str, handler: NodeHandler) -> NodeHandler:
    """为节点记录开始、完成、失败与耗时事件。"""

    async def wrapped(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> NodePatch:
        """观测单个节点，不改变其状态补丁或失败语义。"""

        fields = {
            "agent_run_id": str(state["agent_run_id"]),
            "conversation_id": str(state["conversation_id"]),
            "ssh_session_id": str(state["ssh_session_id"]),
            "api_config_id": str(state["api_config_id"]),
            "api_type": runtime.context.api_config.api_type.value,
            "model": runtime.context.api_config.model,
            "node": node,
            "react_iteration": state["react_iteration"],
        }
        started = time.monotonic_ns()
        LOGGER.debug(
            "agent_node_started fields=%s",
            fields,
            extra={"harness_event": "agent_node_started", "harness_fields": fields},
        )
        try:
            result = handler(state, runtime)
            patch = await result if inspect.isawaitable(result) else result
        except (asyncio.CancelledError, AgentCancelled):
            # 取消是控制流，继续传播给 Service 持久化 CANCELLED；不记作节点失败。
            LOGGER.info(
                "agent_node_cancelled fields=%s",
                fields,
                extra={"harness_event": "agent_node_cancelled", "harness_fields": fields},
            )
            raise
        except GraphInterrupt:
            LOGGER.debug("agent_node_interrupted node=%s", node)
            raise
        except BaseException as error:
            # 结构化字段只收集稳定元数据；当前异常日志仍附带 traceback，
            # 其中可能包含 Provider 正文、命令或远程输出，不能视为安全过滤。
            error_code = getattr(error, "error_code", "SIDECAR_RUNTIME_FAILED")
            if not isinstance(error_code, str):
                error_code = "SIDECAR_RUNTIME_FAILED"
            safe_message = getattr(error, "safe_message", None)
            reason = (
                safe_message
                if isinstance(safe_message, str)
                else f"unexpected {type(error).__module__}.{type(error).__qualname__}"
            )
            LOGGER.exception(
                "agent_node_failed error_code=%s reason=%s fields=%s",
                error_code,
                reason,
                fields,
                extra={
                    "harness_event": "agent_node_failed",
                    "harness_fields": {
                        **fields,
                        "error_code": error_code,
                        "reason": reason,
                    },
                },
            )
            raise
        duration_ms = (time.monotonic_ns() - started) // 1_000_000
        LOGGER.debug(
            "agent_node_completed duration_ms=%s fields=%s",
            duration_ms,
            fields,
            extra={
                "harness_event": "agent_node_completed",
                "harness_fields": {
                    **fields,
                    "duration_ms": duration_ms,
                },
            },
        )
        return patch

    return wrapped


def build_agent_graph(
    dependencies: AgentGraphDependencies,
    *, checkpointer: InMemorySaver | None = None,
) -> CompiledStateGraph[AgentGraphState, AgentGraphContext, AgentGraphState, AgentGraphState]:
    """编译有界 ReAct 图，不使用 LangGraph checkpointer。"""

    policy = AgentContextPolicy()
    budget = dependencies.budget if dependencies.budget is not None else ContextBudget(
        load_local_encoding(tokenizer_resource_dir(), policy.tokenizer_encoding), policy)
    compactor = ContextCompactor(dependencies.database, budget, dependencies.gateway, policy=policy)

    async def load_context(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """原子修复中断历史并追加当前 HumanMessage。"""

        # 1. 先原子修复上轮未完成的工具调用并保存本轮用户消息。
        messages = dependencies.context.load_new_turn(
            state["agent_run_id"],
            state["conversation_id"],
            runtime.context.user_message,
        )
        # 2. 同时加载带序号历史和独立摘要，供后续压缩及模型投影使用。
        with dependencies.database.read_session() as session:
            return {"messages": messages,
                    "records": ConversationRepository(session, PlaintextRecordStore(session)).load_context_messages(state["conversation_id"]),
                    "summaries": ContextSummaryRepository(session).load(state["conversation_id"])}

    async def compact_context(
        state: AgentGraphState, runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """本用户轮次最多执行一次追加摘要。"""
        # 本轮仅由加载后的节点进入一次压缩流程；工具循环不经过此节点。
        summaries = await compactor.compact(config=runtime.context.api_config,
            api_key=runtime.context.api_key, records=state["records"], summaries=state["summaries"],
            conversation_id=state["conversation_id"], source_run_id=state["agent_run_id"],
            cancelled=runtime.context.cancelled)
        return {"summaries": summaries}

    def prepare_model_context(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """检查每次请求预算，不在工具循环内执行摘要。"""
        # 1. 每次主调用前检查有效输入预算，工具循环超预算时直接失败。
        estimate = budget.estimate(runtime.context.api_config, state["records"], state["summaries"])
        budget.assert_fits(runtime.context.api_config, estimate.tokens)
        LOGGER.debug("context_budget source=%s tokens=%s run_id=%s", estimate.source, estimate.tokens, state["agent_run_id"])
        # 2. 预算允许后生成模型专用视图，保持 canonical messages 不变。
        return {"model_messages": dependencies.context.project(state["records"], state["summaries"])}

    async def call_model(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """在任何条件工具派发前持久化完整 AIMessage。"""

        # 1. 用已通过预算检查的投影请求本轮主模型。
        message = await dependencies.gateway.invoke(
            runtime.context.api_config,
            runtime.context.api_key,
            state["model_messages"],
            runtime.context.cancelled,
            runtime.context.text_sink,
        )
        # 2. 保存此次请求的摘要版本和配置指纹，供后续 usage 估算判断能否复用。
        message.additional_kwargs["harness_context_anchor"] = {
            "schema_version": 1,
            "context_revision": state["summaries"][-1].revision if state["summaries"] else 0,
            "request_identity": budget.request_identity(runtime.context.api_config),
        }
        # 3. AI 回复先入库，再用真实序号更新 graph 历史，之后才允许路由到工具执行。
        with dependencies.database.write_session() as session:
            sequence = ConversationRepository(session, PlaintextRecordStore(session)).append_message(
                state["agent_run_id"],
                state["conversation_id"],
                message,
            )
        return {"messages": [message], "records": [*state["records"],
            ContextMessage(sequence, state["agent_run_id"], message)]}

    def route_after_model(
        state: AgentGraphState,
    ) -> Literal["check_react_limit", "return_response"]:
        """将完整文本路由到 END，所有工具决策都经过限制门禁。"""

        message = _last_ai_message(state)
        target = "check_react_limit" if message.tool_calls else "return_response"
        LOGGER.debug(
            "agent_route_selected agent_run_id=%s conversation_id=%s "
            "api_config_id=%s react_iteration=%s route_source=%s route_target=%s",
            state["agent_run_id"],
            state["conversation_id"],
            state["api_config_id"],
            state["react_iteration"],
            "call_model",
            target,
            extra={
                "harness_event": "agent_route_selected",
                "harness_fields": {
                    "agent_run_id": str(state["agent_run_id"]),
                    "conversation_id": str(state["conversation_id"]),
                    "api_config_id": str(state["api_config_id"]),
                    "react_iteration": state["react_iteration"],
                    "route_source": "call_model",
                    "route_target": target,
                },
            },
        )
        return target

    async def check_react_limit(
        state: AgentGraphState,
        _runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """拒绝第 129 次决策，或原子记录下一次已完成循环。"""

        if state["react_iteration"] >= 128:
            return {"last_error_code": "REACT_LIMIT_REACHED"}
        with dependencies.database.write_session() as session:
            run = ConversationRepository(session, PlaintextRecordStore(session)).increment_iteration(state["agent_run_id"])
        return {
            "react_iteration": run.react_iteration,
            "last_error_code": None,
        }

    def route_after_limit(
        state: AgentGraphState,
    ) -> Literal["prepare_tool", "reject_limit"]:
        """仅依据显式持久化的业务限制决策路由。"""

        target = (
            "reject_limit"
            if state["last_error_code"] == "REACT_LIMIT_REACHED"
            else "prepare_tool"
        )
        LOGGER.debug(
            "agent_route_selected agent_run_id=%s conversation_id=%s "
            "api_config_id=%s react_iteration=%s route_source=%s route_target=%s",
            state["agent_run_id"],
            state["conversation_id"],
            state["api_config_id"],
            state["react_iteration"],
            "check_react_limit",
            target,
            extra={
                "harness_event": "agent_route_selected",
                "harness_fields": {
                    "agent_run_id": str(state["agent_run_id"]),
                    "conversation_id": str(state["conversation_id"]),
                    "api_config_id": str(state["api_config_id"]),
                    "react_iteration": state["react_iteration"],
                    "route_source": "check_react_limit",
                    "route_target": target,
                },
            },
        )
        return target

    async def prepare_tool(state: AgentGraphState, runtime: Runtime[AgentGraphContext]) -> dict[str, object]:
        """冻结一次操作，所有校验与 ID 生成在中断节点之前完成。"""
        calls = _last_ai_message(state).tool_calls
        results: list[ToolMessage] = []
        pending: dict[str, object] | None = None
        if len(calls) > 1:
            results = [tool_message(call["id"], _failure_envelope(
                "MULTIPLE_TOOL_CALLS_UNSUPPORTED", "Only one tool call is supported per model response.")) for call in calls]
        else:
            call = calls[0]
            if call["name"] != "execute_command":
                results = [tool_message(call["id"], _failure_envelope("UNKNOWN_TOOL", "The requested tool is not registered."))]
            else:
                try:
                    arguments = ExecuteCommandArguments.model_validate(call["args"])
                except ValidationError:
                    results = [tool_message(call["id"], _failure_envelope("COMMAND_REJECTED_INVALID_ARGUMENTS", "The execute_command arguments are invalid."))]
                else:
                    disposition = classify_command(arguments.command)
                    if disposition == "BLOCKED":
                        results = [tool_message(call["id"], _failure_envelope("COMMAND_REJECTED_DANGEROUS_PATTERN", "The command matched a blocked direct-danger pattern."))]
                    elif disposition == "REQUIRE_APPROVAL":
                        if runtime.context.approval_target is None or runtime.context.approval_registry is None:
                            raise RuntimeError("approval runtime context is required")
                        pending = ApprovalRequest(approval_id=uuid4(), conversation_id=state["conversation_id"],
                            agent_run_id=state["agent_run_id"], ssh_session_id=state["ssh_session_id"],
                            tool_call_id=call["id"], arguments=arguments,
                            target=runtime.context.approval_target).model_dump(mode="json")
        return {"pending_approval": pending, "approval_decision": None, "tool_results": results}

    def route_prepared(state: AgentGraphState) -> Literal["record_tool_result", "await_approval", "execute_tool"]:
        """只将确实可执行或需要审核的调用送向对应节点。"""
        if state["tool_results"]:
            return "record_tool_result"
        return "await_approval" if state["pending_approval"] is not None else "execute_tool"

    def await_approval(state: AgentGraphState, runtime: Runtime[AgentGraphContext]) -> dict[str, object]:
        """纯审核节点可安全从头重跑，不发布事件或执行任何 I/O。"""
        request = ApprovalRequest.model_validate_json(json.dumps(state["pending_approval"]))
        resumed = interrupt(request.model_dump(mode="json"))
        if not isinstance(resumed, dict) or set(resumed) != {"approval_id", "decision"}:
            raise RuntimeError("invalid approval resume fields")
        if resumed["approval_id"] != str(request.approval_id) or resumed["decision"] not in ("approve", "reject"):
            raise RuntimeError("invalid approval resume identity or decision")
        results = [] if resumed["decision"] == "approve" else [tool_message(request.tool_call_id,
            _failure_envelope("COMMAND_REJECTED_BY_USER", "The user rejected this command; it was not executed. Find another approach without bypassing the rejection."))]
        return {"approval_decision": resumed["decision"], "tool_results": results}

    def route_approved(state: AgentGraphState) -> Literal["execute_tool", "record_tool_result"]:
        """拒绝只写工具结果，通过才进入执行节点。"""
        return "record_tool_result" if state["tool_results"] else "execute_tool"

    async def execute_tool(state: AgentGraphState, runtime: Runtime[AgentGraphContext]) -> dict[str, object]:
        """消费当前操作授权后只派发一次，不在本节点设置 interrupt。"""
        if runtime.context.cancelled.is_set():
            raise AgentCancelled()
        call = _last_ai_message(state).tool_calls[0]
        if state["pending_approval"] is not None:
            request = ApprovalRequest.model_validate_json(json.dumps(state["pending_approval"]))
            # 恢复状态也必须与用户看到的原操作完全一致；授权不能移给其他调用。
            if (request.ssh_session_id != state["ssh_session_id"] or request.tool_call_id != call["id"]
                    or call["name"] != "execute_command" or call["args"] != request.arguments.model_dump()):
                raise RuntimeError("approved operation differs from execution input")
            if runtime.context.approval_registry is None:
                raise RuntimeError("approval registry is required")
            runtime.context.approval_registry.consume(request)
        envelope = await _execute_one_tool_call(call, state["ssh_session_id"], runtime.context.cancelled,
            dependencies, runtime.context.text_sink)
        return {"tool_results": [tool_message(call["id"], envelope)]}

    async def record_tool_result(state: AgentGraphState, runtime: Runtime[AgentGraphContext]) -> dict[str, object]:
        """单次短事务保存配对结果，随后清空上一次工具的临时授权状态。"""
        messages = state["tool_results"]
        with dependencies.database.write_session() as session:
            sequences = ConversationRepository(session, PlaintextRecordStore(session)).append_messages_atomic(
                state["agent_run_id"], state["conversation_id"], messages)
        return {"messages": messages, "records": [*state["records"],
            *(ContextMessage(sequence, state["agent_run_id"], message)
              for sequence, message in zip(sequences, messages, strict=True))],
            "pending_approval": None, "approval_decision": None, "tool_results": []}

    async def return_response(
        state: AgentGraphState,
        _runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """报告最终模型文本，由 AgentService 校验传输预算。"""

        message = _last_ai_message(state)
        if message.tool_calls:
            raise RuntimeError("return_response received an AIMessage with tool calls")
        return {
            "run_status": AgentRunStatus.COMPLETED,
            "last_error_code": None,
        }

    async def reject_limit(
        state: AgentGraphState,
        _runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """为每个被拒绝调用配对结果并结束，不再调用 SSH 或模型。"""

        # 1. 给每个未执行调用配对明确的业务上限失败，保持工具历史闭合。
        messages = [
            tool_message(
                call["id"],
                _failure_envelope(
                    "REACT_LIMIT_REACHED",
                    "The Agent reached the 128-iteration ReAct limit.",
                ),
            )
            for call in _last_ai_message(state).tool_calls
        ]
        # 2. 先持久化拒绝消息，再把 Run 转为 LIMIT_REACHED。
        with dependencies.database.write_session() as session:
            sequences = ConversationRepository(session, PlaintextRecordStore(session)).append_messages_atomic(
                state["agent_run_id"],
                state["conversation_id"],
                messages,
            )
        with dependencies.database.write_session() as session:
            ConversationRepository(session, PlaintextRecordStore(session)).finish_run(
                state["agent_run_id"],
                AgentRunStatus.LIMIT_REACHED,
                "REACT_LIMIT_REACHED",
            )
        # 3. 返回终态补丁，不再调用模型或 SSH。
        return {
            "messages": messages,
            "run_status": AgentRunStatus.LIMIT_REACHED,
            "last_error_code": "REACT_LIMIT_REACHED",
        }

    builder = StateGraph(AgentGraphState, context_schema=AgentGraphContext)
    builder.add_node("load_context", _instrument_agent_node("load_context", load_context))
    builder.add_node("compact_context", _instrument_agent_node("compact_context", compact_context))
    builder.add_node("prepare_model_context", _instrument_agent_node("prepare_model_context", prepare_model_context))
    builder.add_node("call_model", _instrument_agent_node("call_model", call_model))
    builder.add_node(
        "check_react_limit",
        _instrument_agent_node("check_react_limit", check_react_limit),
    )
    builder.add_node("prepare_tool", _instrument_agent_node("prepare_tool", prepare_tool))
    builder.add_node("await_approval", _instrument_agent_node("await_approval", await_approval))
    builder.add_node("record_tool_result", _instrument_agent_node("record_tool_result", record_tool_result))
    builder.add_node("execute_tool", _instrument_agent_node("execute_tool", execute_tool))
    builder.add_node(
        "return_response",
        _instrument_agent_node("return_response", return_response),
    )
    builder.add_node("reject_limit", _instrument_agent_node("reject_limit", reject_limit))
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "compact_context")
    builder.add_edge("compact_context", "prepare_model_context")
    builder.add_edge("prepare_model_context", "call_model")
    builder.add_conditional_edges("call_model", route_after_model)
    builder.add_conditional_edges("check_react_limit", route_after_limit)
    builder.add_conditional_edges("prepare_tool", route_prepared)
    builder.add_conditional_edges("await_approval", route_approved)
    builder.add_edge("execute_tool", "record_tool_result")
    builder.add_edge("record_tool_result", "prepare_model_context")
    builder.add_edge("return_response", END)
    builder.add_edge("reject_limit", END)
    return builder.compile(checkpointer=checkpointer)


async def _execute_one_tool_call(
    call: dict[str, Any],
    ssh_session_id: UUID,
    cancelled: asyncio.Event,
    dependencies: AgentGraphDependencies,
    event_sink: AgentTurnEventSink,
) -> CommandToolEnvelope:
    """校验、审查并执行且仅执行一个规范模型工具调用。"""

    if call["name"] != "execute_command":
        return _failure_envelope("UNKNOWN_TOOL", "The requested tool is not registered.")
    try:
        arguments = ExecuteCommandArguments.model_validate(call["args"])
    except ValidationError:
        return _failure_envelope(
            "COMMAND_REJECTED_INVALID_ARGUMENTS",
            "The execute_command arguments are invalid.",
        )
    try:
        dependencies.reviewer.review(arguments.command)
    except CommandRejected as error:
        return _failure_envelope(
            error.error_code,
            "The command matched a blocked direct-danger pattern.",
        )
    # 只有通过校验和审查的调用才发布状态；发布失败时不执行远端命令。
    await event_sink.tool_started(call["id"], arguments)
    return await dependencies.executor.execute(
        ssh_session_id,
        arguments.command,
        cancelled,
    )


def _last_ai_message(state: AgentGraphState) -> AIMessage:
    """返回图中最新的 AIMessage；路由非法时立即失败。"""

    if not state["messages"] or not isinstance(state["messages"][-1], AIMessage):
        raise RuntimeError("Agent graph expected the latest message to be AIMessage")
    return state["messages"][-1]


def _failure_envelope(code: str, message: str) -> CommandToolEnvelope:
    """构建稳定、非敏感的 ToolMessage 失败载荷。"""

    return CommandToolEnvelope(
        ok=False,
        code=code,
        message=message,
        result=None,
    )
