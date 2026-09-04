"""Top-level Agent turn lifecycle and per-conversation serialization."""

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
from .streaming import AgentTurnEventSink
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
LOGGER = logging.getLogger("harness_shell_sidecar.agent.service")


class AgentServiceError(RuntimeError):
    """Expose a stable failure which occurs before a durable Run can start."""

    def __init__(self, error_code: str, message: str) -> None:
        """Store the public code and one reviewed non-sensitive failure reason."""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # Stable handler-facing failure code.
        self.safe_message = message  # Reviewed detail without secret material.


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
        event_sink: AgentTurnEventSink,
    ) -> AgentTurnResult:
        """Run one turn while publishing lifecycle around durable Run states."""

        self._validate_run_authorities(request, expected_config, cancelled)

        conversation_id = request.conversation_id
        if conversation_id is None:
            conversation_id = self._conversations.create_conversation()
        elif not self._conversations.conversation_exists(conversation_id):
            raise AgentServiceError(
                "AGENT_CONVERSATION_NOT_FOUND",
                "the requested conversation does not exist",
            )

        async with self._conversation_lock(conversation_id):
            config = self._validate_run_authorities(
                request,
                expected_config,
                cancelled,
            )
            started_ns = time.monotonic_ns()
            run = self._conversations.start_run(
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
                text_sink=event_sink,
            )
            try:
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
                    finished = self._conversations.finish_run(
                        run.agent_run_id,
                        AgentRunStatus.COMPLETED,
                        None,
                    )
                    _log_terminal_run(finished, config, started_ns)
                    await event_sink.completed(finished)
                    return _result_from_run(finished, final_text=final_text)
            except asyncio.CancelledError:
                # A dispatcher shutdown still requires a durable terminal Run.
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
                await event_sink.failed(finished)
                return _result_from_run(finished, final_text=None)
            except AgentServiceError as error:
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.FAILED,
                    error.error_code,
                )
                _log_terminal_run(finished, config, started_ns)
                await event_sink.failed(finished)
                return _result_from_run(finished, final_text=None)
            except Exception as error:
                error_code = getattr(error, "error_code", "SIDECAR_RUNTIME_FAILED")
                if error_code not in _PUBLIC_RUN_FAILURE_CODES:
                    error_code = "SIDECAR_RUNTIME_FAILED"
                finished = self._finish_if_running(
                    run,
                    AgentRunStatus.FAILED,
                    error_code,
                )
                _log_terminal_run(finished, config, started_ns)
                await event_sink.failed(finished)
                return _result_from_run(finished, final_text=None)

            finished = self._conversations.get_run(run.agent_run_id)
            if finished is None or finished.status is AgentRunStatus.RUNNING:
                raise RuntimeError("Agent graph returned without a durable terminal Run")
            _log_terminal_run(finished, config, started_ns)
            final_text = _final_text(state) if finished.status is AgentRunStatus.COMPLETED else None
            if finished.status is AgentRunStatus.COMPLETED:
                if event_sink.streamed_text != final_text:
                    raise RuntimeError("durable final text does not match streamed text")
                await event_sink.completed(finished)
            else:
                await event_sink.failed(finished)
            return _result_from_run(finished, final_text=final_text)

    def _validate_run_authorities(
        self,
        request: AgentTurnInput,
        expected_config: ModelApiConfig | None,
        cancelled: asyncio.Event,
    ) -> ModelApiConfig:
        """Recheck cancellation, the full config snapshot, and live Session authority."""

        if cancelled.is_set():
            raise AgentServiceError(
                "AGENT_CANCELLED",
                "the turn was cancelled before its run authorities were validated",
            )
        config = self._api_configs.get(request.api_config_id)
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


def _log_terminal_run(
    run: AgentRun,
    config: ModelApiConfig,
    started_ns: int,
) -> None:
    """Emit exactly one lifecycle event for a known durable terminal Run."""

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
    """Extract final text from string or standard Responses content blocks."""

    messages = state.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RuntimeError("completed Agent state does not contain messages")
    message = messages[-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        raise RuntimeError("completed Agent state does not end in final AI text")
    return str(message.text)


def _result_from_run(run: AgentRun, *, final_text: str | None) -> AgentTurnResult:
    """Project one durable Run snapshot into the bounded internal result."""

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
    """Keep the pre-SSE complete-result logical byte budget unchanged."""

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
