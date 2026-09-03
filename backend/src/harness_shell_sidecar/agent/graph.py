"""Custom LangGraph ReAct loop with explicit persistence and business limits."""

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

from harness_shell_sidecar.telemetry import log_event, log_exception_event

from .context import ContextService
from .contracts import (
    AgentRunStatus,
    CommandToolEnvelope,
    ExecuteCommandArguments,
    ModelApiConfig,
)
from .conversations import ConversationRepository
from .tools import (
    CommandRejected,
    CommandSafetyReviewer,
    tool_message,
)


LOGGER = logging.getLogger("harness_shell_sidecar.agent.graph")


class AgentGraphState(TypedDict):
    """Persistable-free state exchanged between explicit Agent graph nodes."""

    agent_run_id: UUID
    conversation_id: UUID
    ssh_session_id: UUID
    api_config_id: UUID
    messages: Annotated[list[AnyMessage], add_messages]
    model_messages: list[AnyMessage]
    react_iteration: int
    run_status: AgentRunStatus
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class AgentGraphContext:
    """Hold sensitive or run-local values outside graph state and persistence."""

    #: Frozen non-secret provider configuration for this Run.
    api_config: ModelApiConfig
    #: Short-lived provider secret supplied only to the current graph invocation.
    api_key: SecretStr
    #: Caller-owned cancellation event shared with model and SSH operations.
    cancelled: asyncio.Event
    #: Current user input persisted by load_context.
    user_message: str


class ModelInvoker(Protocol):
    """Describe the model gateway surface required by graph nodes."""

    async def invoke(
        self,
        config: ModelApiConfig,
        api_key: SecretStr,
        messages: Sequence[AnyMessage],
        cancelled: asyncio.Event,
    ) -> AIMessage:
        """Return one complete model message."""


class CommandExecutor(Protocol):
    """Describe the bound SSH executor surface required by the tool node."""

    async def execute(
        self,
        ssh_session_id: UUID,
        command: str,
        cancelled: asyncio.Event,
    ) -> CommandToolEnvelope:
        """Execute one reviewed command and return its stable envelope."""


@dataclass(frozen=True, slots=True)
class AgentGraphDependencies:
    """Collect long-lived non-secret collaborators captured by the compiled graph."""

    #: Full encrypted history and Run lifecycle authority.
    conversations: ConversationRepository
    #: Interrupted-history repair and model-window projection.
    context: ContextService
    #: Dual-API model invocation gateway.
    gateway: ModelInvoker
    #: Fixed-regex command reviewer.
    reviewer: CommandSafetyReviewer
    #: Frozen-session non-PTY SSH command executor.
    executor: CommandExecutor


NodePatch = dict[str, object]
NodeHandler = Callable[
    [AgentGraphState, Runtime[AgentGraphContext]],
    NodePatch | Awaitable[NodePatch],
]


def _instrument_agent_node(node: str, handler: NodeHandler) -> NodeHandler:
    """Wrap one node with safe start, completion, failure, and duration events."""

    async def wrapped(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> NodePatch:
        """Observe one node without changing its patch or failure semantics."""

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
        log_event(LOGGER, logging.INFO, "agent_node_started", **fields)
        try:
            result = handler(state, runtime)
            patch = await result if inspect.isawaitable(result) else result
        except BaseException as error:
            # Cancellation and system-exit signals are logged only at this
            # boundary and immediately re-raised; the graph keeps ownership.
            error_code = getattr(error, "error_code", "SIDECAR_RUNTIME_FAILED")
            if not isinstance(error_code, str):
                error_code = "SIDECAR_RUNTIME_FAILED"
            log_exception_event(
                LOGGER,
                "agent_node_failed",
                error,
                error_code=error_code,
                **fields,
            )
            raise
        log_event(
            LOGGER,
            logging.INFO,
            "agent_node_completed",
            duration_ms=(time.monotonic_ns() - started) // 1_000_000,
            **fields,
        )
        return patch

    return wrapped


def build_agent_graph(
    dependencies: AgentGraphDependencies,
) -> CompiledStateGraph[AgentGraphState, AgentGraphContext, AgentGraphState, AgentGraphState]:
    """Compile the bounded ReAct graph without any LangGraph checkpointer."""

    async def load_context(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """Repair interrupted history and append the current HumanMessage atomically."""

        messages = dependencies.context.load_new_turn(
            state["agent_run_id"],
            state["conversation_id"],
            runtime.context.user_message,
        )
        return {"messages": messages}

    def trim_context(
        state: AgentGraphState,
        _runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """Build the current System plus last-five-turn model view."""

        return {
            "model_messages": dependencies.context.trim_for_model(state["messages"])
        }

    async def call_model(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """Persist the full AIMessage before any conditional tool dispatch."""

        message = await dependencies.gateway.invoke(
            runtime.context.api_config,
            runtime.context.api_key,
            state["model_messages"],
            runtime.context.cancelled,
        )
        dependencies.conversations.append_message(
            state["agent_run_id"],
            state["conversation_id"],
            message,
        )
        return {"messages": [message]}

    def route_after_model(
        state: AgentGraphState,
    ) -> Literal["check_react_limit", "return_response"]:
        """Route complete text to END and every tool decision through the limit gate."""

        message = _last_ai_message(state)
        target = "check_react_limit" if message.tool_calls else "return_response"
        log_event(
            LOGGER,
            logging.INFO,
            "agent_route_selected",
            agent_run_id=str(state["agent_run_id"]),
            conversation_id=str(state["conversation_id"]),
            api_config_id=str(state["api_config_id"]),
            react_iteration=state["react_iteration"],
            route_source="call_model",
            route_target=target,
        )
        return target

    async def check_react_limit(
        state: AgentGraphState,
        _runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """Reject a 129th decision or atomically count the next completed loop."""

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
        """Route solely from the explicit persisted business-limit decision."""

        target = (
            "reject_limit"
            if state["last_error_code"] == "REACT_LIMIT_REACHED"
            else "execute_tool"
        )
        log_event(
            LOGGER,
            logging.INFO,
            "agent_route_selected",
            agent_run_id=str(state["agent_run_id"]),
            conversation_id=str(state["conversation_id"]),
            api_config_id=str(state["api_config_id"]),
            react_iteration=state["react_iteration"],
            route_source="check_react_limit",
            route_target=target,
        )
        return target

    async def execute_tool(
        state: AgentGraphState,
        runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """Pair model calls with structured results and dispatch at most one command."""

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
            envelope = await _execute_one_tool_call(
                call,
                state["ssh_session_id"],
                runtime.context.cancelled,
                dependencies,
            )
            messages = [tool_message(call["id"], envelope)]
        dependencies.conversations.append_messages_atomic(
            state["agent_run_id"],
            state["conversation_id"],
            messages,
        )
        return {"messages": messages}

    async def return_response(
        state: AgentGraphState,
        _runtime: Runtime[AgentGraphContext],
    ) -> dict[str, object]:
        """Report final model text while AgentService validates transport budget."""

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
        """Pair every refused call and finish without invoking SSH or the model again."""

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
        dependencies.conversations.append_messages_atomic(
            state["agent_run_id"],
            state["conversation_id"],
            messages,
        )
        dependencies.conversations.finish_run(
            state["agent_run_id"],
            AgentRunStatus.LIMIT_REACHED,
            "REACT_LIMIT_REACHED",
        )
        return {
            "messages": messages,
            "run_status": AgentRunStatus.LIMIT_REACHED,
            "last_error_code": "REACT_LIMIT_REACHED",
        }

    builder = StateGraph(AgentGraphState, context_schema=AgentGraphContext)
    builder.add_node("load_context", _instrument_agent_node("load_context", load_context))
    builder.add_node("trim_context", _instrument_agent_node("trim_context", trim_context))
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
    builder.add_edge("load_context", "trim_context")
    builder.add_edge("trim_context", "call_model")
    builder.add_conditional_edges("call_model", route_after_model)
    builder.add_conditional_edges("check_react_limit", route_after_limit)
    builder.add_edge("execute_tool", "trim_context")
    builder.add_edge("return_response", END)
    builder.add_edge("reject_limit", END)
    return builder.compile()


async def _execute_one_tool_call(
    call: dict[str, Any],
    ssh_session_id: UUID,
    cancelled: asyncio.Event,
    dependencies: AgentGraphDependencies,
) -> CommandToolEnvelope:
    """Validate, review, and execute exactly one canonical model tool call."""

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
    """Return the graph's latest AIMessage or expose an invalid route immediately."""

    if not state["messages"] or not isinstance(state["messages"][-1], AIMessage):
        raise RuntimeError("Agent graph expected the latest message to be AIMessage")
    return state["messages"][-1]


def _failure_envelope(code: str, message: str) -> CommandToolEnvelope:
    """Build one stable non-sensitive ToolMessage failure payload."""

    return CommandToolEnvelope(
        ok=False,
        code=code,
        message=message,
        result=None,
    )
