"""Top-level Agent turn lifecycle and per-conversation serialization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage
from pydantic import SecretStr

from harness_shell_sidecar.protocol import (
    MAX_PAYLOAD_BYTES,
    FrameEnvelope,
    MessageType,
    Sensitivity,
)

from .api_configs import ApiConfigRepository
from .context import ContextService
from .contracts import (
    AgentRun,
    AgentRunStatus,
    AgentTurnInput,
    AgentTurnResult,
    ModelApiConfig,
)
from .conversations import ConversationRepository
from .executor import AgentCancelled
from .graph import (
    AgentGraphContext,
    AgentGraphDependencies,
    AgentGraphState,
    CommandExecutor,
    ModelInvoker,
    build_agent_graph,
)
from .tools import CommandSafetyReviewer


_PUBLIC_RUN_FAILURE_CODES = frozenset(
    {
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


class AgentServiceError(RuntimeError):
    """Expose a stable failure which occurs before a durable Run can start."""

    def __init__(self, error_code: str) -> None:
        """Store the public code without including request or secret material."""

        super().__init__(error_code)
        self.error_code = error_code  # Stable handler-facing failure code.


@dataclass(slots=True)
class _ConversationLockEntry:
    """Track one conversation lock and every current holder or waiter."""

    lock: asyncio.Lock
    users: int = 0


class AgentService:
    """Validate a turn, serialize its conversation, and own graph lifecycle mapping."""

    def __init__(
        self,
        api_configs: ApiConfigRepository,
        conversations: ConversationRepository,
        executor: CommandExecutor,
        gateway: ModelInvoker,
        context: ContextService,
        session_is_available: Callable[[UUID], bool],
    ) -> None:
        """Build one reusable graph from long-lived non-secret runtime collaborators."""

        self._api_configs = api_configs  # Provider configuration authority.
        self._conversations = conversations  # History and Run authority.
        self._session_is_available = session_is_available
        self._conversation_locks: dict[UUID, _ConversationLockEntry] = {}
        dependencies = AgentGraphDependencies(
            conversations=conversations,
            context=context,
            gateway=gateway,
            reviewer=CommandSafetyReviewer(),
            executor=executor,
        )
        self._graph = build_agent_graph(dependencies)  # Compiled without checkpointer.

    async def run_turn(
        self,
        request: AgentTurnInput,
        api_key: SecretStr,
        cancelled: asyncio.Event,
        *,
        expected_config: ModelApiConfig | None,
    ) -> AgentTurnResult:
        """Run one non-streaming turn and return its durable terminal snapshot."""

        self._validate_run_authorities(request, expected_config, cancelled)

        conversation_id = request.conversation_id
        if conversation_id is None:
            conversation_id = self._conversations.create_conversation()
        elif not self._conversations.conversation_exists(conversation_id):
            raise AgentServiceError("AGENT_CONVERSATION_NOT_FOUND")

        async with self._conversation_lock(conversation_id):
            config = self._validate_run_authorities(
                request,
                expected_config,
                cancelled,
            )
            run = self._conversations.start_run(
                conversation_id,
                request.ssh_session_id,
                request.api_config_id,
            )
            initial_state: AgentGraphState = {
                "agent_run_id": run.agent_run_id,
                "conversation_id": conversation_id,
                "ssh_session_id": request.ssh_session_id,
                "api_config_id": request.api_config_id,
                "messages": [],
                "model_messages": [],
                "react_iteration": 0,
                "run_status": AgentRunStatus.RUNNING,
                "last_error_code": None,
            }
            graph_context = AgentGraphContext(
                api_config=config,
                api_key=api_key,
                cancelled=cancelled,
                user_message=request.user_message,
            )
            try:
                state = await self._graph.ainvoke(
                    initial_state,
                    config={"recursion_limit": 1024},
                    context=graph_context,
                )
                if state["run_status"] is AgentRunStatus.COMPLETED:
                    final_text = _final_text(state)
                    _require_agent_result_fits(
                        run,
                        final_text,
                        react_iteration=state["react_iteration"],
                    )
                    finished = self._conversations.finish_run(
                        run.agent_run_id,
                        AgentRunStatus.COMPLETED,
                        None,
                    )
                    return _result_from_run(finished, final_text=final_text)
            except asyncio.CancelledError:
                # A dispatcher shutdown still requires a durable terminal Run.
                self._finish_if_running(
                    run,
                    AgentRunStatus.CANCELLED,
                    "AGENT_CANCELLED",
                )
                raise
            except AgentCancelled as error:
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.CANCELLED,
                    error.error_code,
                )
                return _result_from_run(finished, final_text=None)
            except AgentServiceError as error:
                self._finish_if_running(
                    run,
                    AgentRunStatus.FAILED,
                    error.error_code,
                )
                raise
            except Exception as error:
                error_code = getattr(error, "error_code", "SIDECAR_RUNTIME_FAILED")
                if error_code not in _PUBLIC_RUN_FAILURE_CODES:
                    error_code = "SIDECAR_RUNTIME_FAILED"
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.FAILED,
                    error_code,
                )
                return _result_from_run(finished, final_text=None)

            finished = self._conversations.get_run(run.agent_run_id)
            if finished is None or finished.status is AgentRunStatus.RUNNING:
                raise RuntimeError("Agent graph returned without a durable terminal Run")
            final_text = _final_text(state) if finished.status is AgentRunStatus.COMPLETED else None
            return _result_from_run(finished, final_text=final_text)

    def _validate_run_authorities(
        self,
        request: AgentTurnInput,
        expected_config: ModelApiConfig | None,
        cancelled: asyncio.Event,
    ) -> ModelApiConfig:
        """Recheck cancellation, the full config snapshot, and live Session authority."""

        if cancelled.is_set():
            raise AgentServiceError("AGENT_CANCELLED")
        config = self._api_configs.get(request.api_config_id)
        if config is None:
            raise AgentServiceError("MODEL_API_CONFIG_NOT_FOUND")
        if not config.enabled:
            raise AgentServiceError("MODEL_API_CONFIG_DISABLED")
        if expected_config is None or config != expected_config:
            raise AgentServiceError("MODEL_API_CONFIG_CHANGED")
        if not self._session_is_available(request.ssh_session_id):
            raise AgentServiceError("SSH_SESSION_UNAVAILABLE")
        return config

    @asynccontextmanager
    async def _conversation_lock(
        self,
        conversation_id: UUID,
    ) -> AsyncIterator[None]:
        """Serialize a conversation and remove its lock after the final user exits."""

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
        """Apply one failure transition unless a graph node already reached terminal state."""

        current = self._conversations.get_run(original_run.agent_run_id)
        if current is None:
            raise RuntimeError("Agent run disappeared during terminal failure mapping")
        if current.status is not AgentRunStatus.RUNNING:
            return current
        return self._conversations.finish_run(
            original_run.agent_run_id,
            status,
            error_code,
        )


def _final_text(state: dict[str, Any]) -> str:
    """Extract final text from string or standard Responses content blocks."""

    messages = state.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RuntimeError("completed Agent state does not contain messages")
    message = messages[-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        raise RuntimeError("completed Agent state does not end in final AI text")
    return str(message.text)


def _result_from_run(run: AgentRun, *, final_text: str | None) -> AgentTurnResult:
    """Project one durable Run snapshot into the strict public turn result."""

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
    """Validate a successful result against the worst legal Protocol envelope."""

    result = AgentTurnResult(
        conversation_id=run.conversation_id,
        agent_run_id=run.agent_run_id,
        status=AgentRunStatus.COMPLETED,
        final_text=final_text,
        react_iteration=react_iteration,
        error_code=None,
    )
    candidate = FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.RESPONSE,
        request_id=uuid4(),
        task_id=uuid4(),
        workflow_run_id=uuid4(),
        sequence=(2**64) - 1,
        timestamp=datetime.now(timezone.utc),
        sensitivity=Sensitivity.NORMAL,
        payload=result.model_dump(mode="json"),
    )
    encoded_size = len(candidate.model_dump_json(exclude_none=False).encode("utf-8"))
    if encoded_size > MAX_PAYLOAD_BYTES:
        raise AgentServiceError("AGENT_RESPONSE_TOO_LARGE")
