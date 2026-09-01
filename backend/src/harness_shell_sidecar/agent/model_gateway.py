"""Explicit dual-API model construction and timeout-only retry policy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from harness_shell_sidecar.telemetry import log_exception_event
from .contracts import ApiType, ModelApiConfig
from .executor import AgentCancelled
from .tools import build_execute_command_schema_tool

MODEL_REQUEST_TIMEOUT_SECONDS = 60
MODEL_RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16)
ModelBuilder = Callable[..., Any]
Sleep = Callable[[float], Awaitable[None]]
LOGGER = logging.getLogger("harness_shell_sidecar.agent.model_gateway")


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
    ) -> AIMessage:
        """Return an AIMessage or raise one stable terminal gateway error."""

        if cancelled.is_set():
            raise AgentCancelled()
        model = self._factory.create(config, api_key)
        for attempt in range(len(MODEL_RETRY_DELAYS_SECONDS) + 1):
            try:
                value = await _await_with_cancellation(
                    self.model_invoke(model, messages),
                    cancelled,
                )
            except AgentCancelled:
                raise
            except Exception as error:
                if not _is_network_timeout(error):
                    log_exception_event(
                        LOGGER,
                        "model_request_failed",
                        error,
                        error_code="MODEL_REQUEST_FAILED",
                        api_config_id=str(config.api_config_id),
                        api_type=config.api_type.value,
                        model=config.model,
                    )
                    raise ModelGatewayError("MODEL_REQUEST_FAILED") from error
                if attempt == len(MODEL_RETRY_DELAYS_SECONDS):
                    log_exception_event(
                        LOGGER,
                        "model_network_timeout",
                        error,
                        error_code="MODEL_NETWORK_TIMEOUT",
                        api_config_id=str(config.api_config_id),
                        api_type=config.api_type.value,
                        model=config.model,
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
    ) -> AIMessage | None:
        """Aggregate one model stream into a complete AIMessage."""

        full_message: AIMessageChunk | None = None

        async for chunk in model.astream(messages):
            if not isinstance(chunk, AIMessageChunk):
                return None
            full_message = (
                chunk
                if full_message is None
                else full_message + chunk
            )

        if full_message is None:
            return None
        message = message_chunk_to_message(full_message)
        return message if isinstance(message, AIMessage) else None

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
