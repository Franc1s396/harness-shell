"""具有显式持久化与业务限制的自定义 LangGraph ReAct 循环。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypedDict
from uuid import UUID

from langchain_core.messages import AIMessage, AnyMessage
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
from .streaming import AgentTextDeltaSink
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
    summary: ContextSummary | None  # 仅供模型使用的滚动摘要，不作为 SSE 可见文本。
    react_iteration: int
    run_status: AgentRunStatus
    last_error_code: str | None


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
    #: 本 Run 局部的可见文本接收端，不进入图状态或持久化。
    text_sink: AgentTextDeltaSink


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
    conversations: ConversationRepository
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
) -> CompiledStateGraph[AgentGraphState, AgentGraphContext, AgentGraphState, AgentGraphState]:
    """编译有界 ReAct 图，不使用 LangGraph checkpointer。"""

    policy = AgentContextPolicy()
    budget = dependencies.budget if dependencies.budget is not None else ContextBudget(
        load_local_encoding(tokenizer_resource_dir(), policy.tokenizer_encoding), policy)
    summaries = ContextSummaryRepository(dependencies.conversations.database)
    compactor = ContextCompactor(summaries, budget, dependencies.gateway)

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
        return {"messages": messages,
                "records": dependencies.conversations.load_context_messages(state["conversation_id"]),
                "summary": summaries.load(state["conversation_id"])}

    async def compact_context(
        state: AgentGraphState, runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """本用户轮次最多执行一次滚动摘要。"""
        # 本轮仅由加载后的节点进入一次压缩流程；工具循环不经过此节点。
        summary = await compactor.compact(config=runtime.context.api_config,
            api_key=runtime.context.api_key, records=state["records"], summary=state["summary"],
            conversation_id=state["conversation_id"], source_run_id=state["agent_run_id"],
            cancelled=runtime.context.cancelled)
        return {"summary": summary}

    def prepare_model_context(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """检查每次请求预算，不在工具循环内执行摘要。"""
        # 1. 每次主调用前检查有效输入预算，工具循环超预算时直接失败。
        estimate = budget.estimate(runtime.context.api_config, state["records"], state["summary"])
        budget.assert_fits(runtime.context.api_config, estimate.tokens)
        LOGGER.debug("context_budget source=%s tokens=%s run_id=%s", estimate.source, estimate.tokens, state["agent_run_id"])
        # 2. 预算允许后生成模型专用视图，保持 canonical messages 不变。
        return {"model_messages": dependencies.context.project(state["records"], state["summary"])}

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
            "context_revision": state["summary"].revision if state["summary"] else 0,
            "request_identity": budget.request_identity(runtime.context.api_config),
        }
        # 3. AI 回复先入库，再用真实序号更新 graph 历史，之后才允许路由到工具执行。
        sequence = dependencies.conversations.append_message(
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
        run = dependencies.conversations.increment_iteration(state["agent_run_id"])
        return {
            "react_iteration": run.react_iteration,
            "last_error_code": None,
        }

    def route_after_limit(
        state: AgentGraphState,
    ) -> Literal["execute_tool", "reject_limit"]:
        """仅依据显式持久化的业务限制决策路由。"""

        target = (
            "reject_limit"
            if state["last_error_code"] == "REACT_LIMIT_REACHED"
            else "execute_tool"
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

    async def execute_tool(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """为模型调用配对结构化结果，最多派发一条命令。"""

        # 1. 读取已持久化的工具决策；多个调用全部配对拒绝结果，不派发命令。
        calls = _last_ai_message(state).tool_calls
        if len(calls) > 1:
            messages = [
                tool_message(
                    call["id"],
                    _failure_envelope(
                        "MULTIPLE_TOOL_CALLS_UNSUPPORTED",
                        "Only one tool call is supported per model response.",
                    ),
                )
                for call in calls
            ]
        else:
            call = calls[0]
            # 2. 单调用进入工具名、参数和安全审查边界后，才可在冻结会话上执行。
            envelope = await _execute_one_tool_call(
                call,
                state["ssh_session_id"],
                runtime.context.cancelled,
                dependencies,
            )
            messages = [tool_message(call["id"], envelope)]
        # 3. 原子保存全部工具结果，再按真实序号更新图历史并继续模型循环。
        sequences = dependencies.conversations.append_messages_atomic(
            state["agent_run_id"],
            state["conversation_id"],
            messages,
        )
        return {"messages": messages, "records": [*state["records"],
            *(ContextMessage(sequence, state["agent_run_id"], message)
              for sequence, message in zip(sequences, messages, strict=True))]}

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
        sequences = dependencies.conversations.append_messages_atomic(
            state["agent_run_id"],
            state["conversation_id"],
            messages,
        )
        dependencies.conversations.finish_run(
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
    builder.add_edge("execute_tool", "prepare_model_context")
    builder.add_edge("return_response", END)
    builder.add_edge("reject_limit", END)
    return builder.compile()


async def _execute_one_tool_call(
    call: dict[str, Any],
    ssh_session_id: UUID,
    cancelled: asyncio.Event,
    dependencies: AgentGraphDependencies,
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
