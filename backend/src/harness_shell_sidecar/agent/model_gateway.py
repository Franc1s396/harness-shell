"""Explicit dual-API model construction and timeout-only retry policy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from harness_shell_sidecar.telemetry import log_event
from .contracts import ApiType, ModelApiConfig
from .executor import AgentCancelled
from .streaming import AgentTextDeltaSink
from .tools import build_execute_command_schema_tool

MODEL_REQUEST_TIMEOUT_SECONDS = 60
MODEL_RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16)
ModelBuilder = Callable[..., Any]
Sleep = Callable[[float], Awaitable[None]]
LOGGER = logging.getLogger("harness_shell_sidecar.agent.model_gateway")


class _InvocationMode(Enum):
    """Track whether one Provider invocation represents a tool call or final text."""

    UNDECIDED = "UNDECIDED"
    TOOL_CALL = "TOOL_CALL"
    FINAL_TEXT = "FINAL_TEXT"


@dataclass(slots=True)
class _InvocationState:
    """Retain per-attempt semantic mode and exact emitted visible fragments."""

    # Semantic choice made by the first tool or non-empty visible delta.
    mode: _InvocationMode = _InvocationMode.UNDECIDED
    # Exact final-text fragments emitted during only this Provider attempt.
    visible_parts: list[str] = field(default_factory=list)


class ModelGatewayError(RuntimeError):
    """Carry a stable non-sensitive code for a terminal model failure."""

    def __init__(self, error_code: str) -> None:
        """Store the public code without copying provider exception text."""

        super().__init__(error_code)
        self.error_code = error_code  # Stable run failure code.


class ChatModelFactory:
    """Construct one ChatOpenAI runnable from an explicit persisted API choice."""

    def __init__(self, model_builder: ModelBuilder = ChatOpenAI) -> None:
        """Allow a typed local fake to replace the provider constructor in tests."""

        self._model_builder = model_builder  # ChatOpenAI-compatible constructor.

    def create(
            self,
            config: ModelApiConfig,
            api_key: SecretStr,
    ) -> Runnable[Any, Any]:
        """Bind the same strict command schema with SDK retries disabled."""

        model = self._model_builder(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
            use_responses_api=config.api_type is ApiType.RESPONSES,
            use_previous_response_id=False,
        )
        return model.bind_tools(
            [build_execute_command_schema_tool()],
            strict=True,
            parallel_tool_calls=False,
        )


class ModelGateway:
    """Invoke one bound model with cancellation and timeout-only retry semantics."""

    def __init__(
            self,
            *,
            model_builder: ModelBuilder = ChatOpenAI,
            sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Bind the explicit model factory and injectable retry sleeper."""

        self._factory = ChatModelFactory(model_builder)  # Builds once per invocation.
        self._sleep = sleep  # Backoff waits remain cancellable by the caller.

    async def invoke(
            self,
            config: ModelApiConfig,
            api_key: SecretStr,
            messages: Sequence[AnyMessage],
            cancelled: asyncio.Event,
            text_sink: AgentTextDeltaSink,
    ) -> AIMessage:
        """Stream final visible text while returning one complete AIMessage."""

        if cancelled.is_set():
            raise AgentCancelled()
        model = self._factory.create(config, api_key)
        for attempt in range(len(MODEL_RETRY_DELAYS_SECONDS) + 1):
            invocation = _InvocationState()
            try:
                value = await _await_with_cancellation(
                    self.model_invoke(model, messages, text_sink, invocation),
                    cancelled,
                )
            except (AgentCancelled, ModelGatewayError):
                raise
            except Exception as error:
                # Stream-owned failures retain their code so AgentService can
                # durably terminalize the Run without mislabeling the Provider.
                if getattr(error, "error_code", None) == "AGENT_RESPONSE_TOO_LARGE":
                    raise
                if not _is_network_timeout(error):
                    log_event(
                        LOGGER,
                        logging.ERROR,
                        "model_request_failed",
                        error_code="MODEL_REQUEST_FAILED",
                        api_config_id=str(config.api_config_id),
                        api_type=config.api_type.value,
                        model=config.model,
                        **_safe_provider_error_fields(error),
                    )
                    raise ModelGatewayError("MODEL_REQUEST_FAILED") from error
                if (
                    invocation.mode is _InvocationMode.FINAL_TEXT
                    or attempt == len(MODEL_RETRY_DELAYS_SECONDS)
                ):
                    log_event(
                        LOGGER,
                        logging.ERROR,
                        "model_network_timeout",
                        error_code="MODEL_NETWORK_TIMEOUT",
                        api_config_id=str(config.api_config_id),
                        api_type=config.api_type.value,
                        model=config.model,
                        **_safe_provider_error_fields(error),
                    )
                    raise ModelGatewayError("MODEL_NETWORK_TIMEOUT") from error
                await _await_with_cancellation(
                    self._sleep(MODEL_RETRY_DELAYS_SECONDS[attempt]),
                    cancelled,
                )
                continue
            if not isinstance(value, AIMessage):
                raise ModelGatewayError("MODEL_RESPONSE_INVALID")
            return value
        raise AssertionError("model retry loop exhausted without a terminal result")

    async def model_invoke(
            self,
            model: Runnable[Any, Any],
            messages: Sequence[AnyMessage],
            text_sink: AgentTextDeltaSink,
            invocation: _InvocationState,
    ) -> AIMessage | None:
        """Aggregate one stream and emit only unambiguous final chunk_text text."""

        full_message: AIMessageChunk | None = None

        async for chunk in model.astream(messages):
            if not isinstance(chunk, AIMessageChunk):
                return None
            chunk_text = str(chunk.text)
            has_tool_delta = bool(chunk.tool_call_chunks or chunk.tool_calls)
            if chunk_text and has_tool_delta:
                raise ModelGatewayError("MODEL_RESPONSE_INVALID")
            if has_tool_delta:
                if invocation.mode is _InvocationMode.FINAL_TEXT:
                    raise ModelGatewayError("MODEL_RESPONSE_INVALID")
                invocation.mode = _InvocationMode.TOOL_CALL
            if chunk_text:
                if invocation.mode is _InvocationMode.TOOL_CALL:
                    raise ModelGatewayError("MODEL_RESPONSE_INVALID")
                invocation.mode = _InvocationMode.FINAL_TEXT
                invocation.visible_parts.append(chunk_text)
                await text_sink.text_delta(chunk_text)
            full_message = (
                chunk
                if full_message is None
                else full_message + chunk
            )

        if full_message is None:
            return None
        message = message_chunk_to_message(full_message)
        if not isinstance(message, AIMessage):
            return None
        if invocation.mode is _InvocationMode.FINAL_TEXT:
            if message.tool_calls or "".join(invocation.visible_parts) != str(message.text):
                raise ModelGatewayError("MODEL_RESPONSE_INVALID")
        if invocation.mode is _InvocationMode.TOOL_CALL and not message.tool_calls:
            raise ModelGatewayError("MODEL_RESPONSE_INVALID")
        return message


def _safe_provider_error_fields(error: BaseException) -> dict[str, object]:
    """Extract allowlisted Provider metadata without exception or response text."""

    error_type = type(error)
    fields: dict[str, object] = {
        "exception_type": f"{error_type.__module__}.{error_type.__qualname__}",
    }
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        fields["http_status"] = status_code
    request_id = getattr(error, "request_id", None)
    if isinstance(request_id, str) and request_id:
        fields["provider_request_id"] = request_id
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested = body.get("error")
        candidate = nested if isinstance(nested, dict) else body
        provider_type = candidate.get("type")
        if isinstance(provider_type, (str, int, float, bool)) or provider_type is None:
            if "type" in candidate:
                fields["provider_error_type"] = provider_type
        provider_code = candidate.get("code")
        if isinstance(provider_code, (str, int, float, bool)) or provider_code is None:
            if "code" in candidate:
                fields["provider_error_code"] = provider_code
    return fields

async def _await_with_cancellation(
        operation: Awaitable[Any],
        cancelled: asyncio.Event,
) -> Any:
    """Race one operation against cancellation and await all cancelled tasks."""

    if cancelled.is_set():
        if asyncio.iscoroutine(operation):
            operation.close()
        raise AgentCancelled()
    operation_task = asyncio.ensure_future(operation)
    cancel_task = asyncio.create_task(cancelled.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and cancelled.is_set():
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise AgentCancelled()
        return await operation_task
    except asyncio.CancelledError:
        # The dispatcher owns the outer Task; do not orphan provider or backoff work.
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise
    finally:
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)


def _is_network_timeout(error: BaseException) -> bool:
    """Classify only typed timeout exceptions across the explicit cause chain."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.TimeoutException, asyncio.TimeoutError)):
            return True
        current = current.__cause__
    return False
