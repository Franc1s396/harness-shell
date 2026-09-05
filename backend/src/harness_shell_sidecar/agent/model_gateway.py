"""Explicit dual-API model construction and timeout-only retry policy."""

from __future__ import annotations

import asyncio
import ast
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal, TypeVar
from uuid import UUID, uuid4

import openai
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessageParam, ChatCompletionToolParam
from openai.types.responses import (
    FunctionToolParam,
    ResponseInputParam,
    ResponseStreamEvent,
)
from pydantic import (
    BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator,
)

from .contracts import ApiType, ModelApiConfig
from .executor import AgentCancelled
from .streaming import AgentTextDeltaSink
from .tools import build_execute_command_tool_definition

MODEL_REQUEST_TIMEOUT_SECONDS = 60
MODEL_RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16)
OpenAIClientBuilder = Callable[..., AsyncOpenAI]
Sleep = Callable[[float], Awaitable[None]]
LOGGER = logging.getLogger("harness_shell_sidecar.agent.model_gateway")
OperationResult = TypeVar("OperationResult")


class _InvocationMode(Enum):
    """Track whether one Provider invocation represents a tool call or final text."""

    UNDECIDED = "UNDECIDED"
    TOOL_CALL = "TOOL_CALL"
    FINAL_TEXT = "FINAL_TEXT"


@dataclass(slots=True)
class _InvocationState:
    """Retain per-attempt semantic mode and exact emitted visible fragments."""

    # Semantic choice made after aggregating the complete Provider invocation.
    mode: _InvocationMode = _InvocationMode.UNDECIDED
    # Exact final-text fragments emitted during only this Provider attempt.
    visible_parts: list[str] = field(default_factory=list)
    # Original publisher failure, retained only to avoid misclassifying its owner.
    sink_error: Exception | None = None


class ModelGatewayError(RuntimeError):
    """Carry a stable non-sensitive code for a terminal model failure."""

    def __init__(self, error_code: str, message: str) -> None:
        """Store the public code and one reviewed non-sensitive failure reason."""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # Stable run failure code.
        self.safe_message = message  # Reviewed detail without Provider content.


class ModelGateway:
    """Own one official SDK client per invocation, including retries and cancellation."""

    def __init__(
            self,
            *,
            client_builder: OpenAIClientBuilder = AsyncOpenAI,
            sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Bind the official client constructor and injectable retry sleeper."""

        self._client_builder = client_builder  # Creates one invocation-owned SDK client.
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
            raise AgentCancelled(
                message="the model request was cancelled before Provider invocation"
            )
        client = self._client_builder(
            api_key=api_key.get_secret_value(), base_url=config.base_url,
            timeout=MODEL_REQUEST_TIMEOUT_SECONDS, max_retries=0,
        )
        async with _close_on_exit(client):
            return await self._invoke_attempts(config, client, messages, cancelled, text_sink)

    async def _invoke_attempts(
        self, config: ModelApiConfig, client: AsyncOpenAI,
        messages: Sequence[AnyMessage], cancelled: asyncio.Event,
        text_sink: AgentTextDeltaSink,
    ) -> AIMessage:
        """Retry only pre-visible timeouts, reusing the invocation's connection pool."""

        for attempt in range(len(MODEL_RETRY_DELAYS_SECONDS) + 1):
            invocation = _InvocationState()
            try:
                value = await _await_with_cancellation(
                    self.model_invoke(config, client, messages, text_sink, invocation),
                    cancelled,
                )
            except (AgentCancelled, ModelGatewayError):
                raise
            except Exception as error:
                # Stream-owned failures retain their code so AgentService can
                # durably terminalize the Run without mislabeling the Provider.
                if invocation.sink_error is error:
                    raise
                if not _is_network_timeout(error):
                    provider_fields = _safe_provider_error_fields(error)
                    LOGGER.exception(
                        "model_request_failed error_code=%s api_config_id=%s "
                        "api_type=%s model=%s provider=%s",
                        "MODEL_REQUEST_FAILED",
                        config.api_config_id,
                        config.api_type.value,
                        config.model,
                        provider_fields,
                        extra={
                            "harness_event": "model_request_failed",
                            "harness_fields": {
                                "error_code": "MODEL_REQUEST_FAILED",
                                "api_config_id": str(config.api_config_id),
                                "api_type": config.api_type.value,
                                "model": config.model,
                                **provider_fields,
                            },
                        },
                    )
                    raise ModelGatewayError(
                        "MODEL_REQUEST_FAILED",
                        "provider request failed before producing a valid response",
                    ) from error
                if (
                    invocation.mode is _InvocationMode.FINAL_TEXT
                    or attempt == len(MODEL_RETRY_DELAYS_SECONDS)
                ):
                    provider_fields = _safe_provider_error_fields(error)
                    LOGGER.exception(
                        "model_network_timeout error_code=%s api_config_id=%s "
                        "api_type=%s model=%s attempt=%s provider=%s",
                        "MODEL_NETWORK_TIMEOUT",
                        config.api_config_id,
                        config.api_type.value,
                        config.model,
                        attempt + 1,
                        provider_fields,
                        extra={
                            "harness_event": "model_network_timeout",
                            "harness_fields": {
                                "error_code": "MODEL_NETWORK_TIMEOUT",
                                "api_config_id": str(config.api_config_id),
                                "api_type": config.api_type.value,
                                "model": config.model,
                                "attempt": attempt + 1,
                                **provider_fields,
                            },
                        },
                    )
                    raise ModelGatewayError(
                        "MODEL_NETWORK_TIMEOUT",
                        "provider request exceeded the configured timeout",
                    ) from error
                await _await_with_cancellation(
                    self._sleep(MODEL_RETRY_DELAYS_SECONDS[attempt]),
                    cancelled,
                )
                continue
            if not isinstance(value, AIMessage):
                raise ModelGatewayError(
                    "MODEL_RESPONSE_INVALID",
                    "provider stream did not produce a complete AI message",
                )
            return value
        raise AssertionError("model retry loop exhausted without a terminal result")

    async def model_invoke(
        self, config: ModelApiConfig, client: AsyncOpenAI,
        messages: Sequence[AnyMessage], text_sink: AgentTextDeltaSink,
        invocation: _InvocationState,
    ) -> AIMessage:
        """Dispatch exactly the configured API without probing or protocol retries."""

        if config.api_type is ApiType.RESPONSES:
            return await _invoke_responses(client, config, messages, text_sink, invocation)
        if config.api_type is ApiType.CHAT_COMPLETIONS:
            return await _invoke_chat_completions(client, config, messages, text_sink, invocation)
        raise AssertionError("unsupported validated API type")


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
        operation: Awaitable[OperationResult],
        cancelled: asyncio.Event,
) -> OperationResult:
    """Race one operation against cancellation and await all cancelled tasks."""

    if cancelled.is_set():
        if asyncio.iscoroutine(operation):
            operation.close()
        raise AgentCancelled(
            message="the async operation was cancelled before it started"
        )
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
            raise AgentCancelled(
                message="the async operation was cancelled while it was running"
            )
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
        if isinstance(current, (openai.APITimeoutError, asyncio.TimeoutError)):
            return True
        current = current.__cause__
    return False


class _ResponsesSummaryReplay(BaseModel):
    """Retain one model-generated reasoning summary item for same-config replay."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["summary_text"] = Field(description="Responses summary discriminator.")
    text: str = Field(description="Model-generated summary returned by the Provider.")


class _ResponsesReasoningReplay(BaseModel):
    """Retain the completed reasoning item required by stateless Tool continuation."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["reasoning"] = Field(description="Responses item discriminator.")
    id: str = Field(description="Provider item identity, scoped to one API config.")
    summary: tuple[_ResponsesSummaryReplay, ...] = Field(
        description="Completed reasoning summaries in Provider order."
    )
    encrypted_content: str | None = Field(
        default=None,
        description="Opaque reasoning replay material; never logged or shown.",
    )
    status: Literal["completed"] = Field(description="Only completed items are replayed.")


class _ResponsesOutputTextReplay(BaseModel):
    """Retain one completed visible Responses text block without raw metadata."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["output_text"] = Field(description="Responses text discriminator.")
    text: str = Field(description="Exact final visible text.")
    annotations: tuple[()] = Field(
        default=(),
        description="Annotations are intentionally not replayed by this Agent.",
    )


class _ResponsesMessageReplay(BaseModel):
    """Retain a completed assistant message and its optional phase."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["message"] = Field(description="Responses item discriminator.")
    id: str = Field(description="Provider item identity, scoped to one API config.")
    role: Literal["assistant"] = Field(description="Only assistant output is replayed.")
    status: Literal["completed"] = Field(description="Only completed output is replayed.")
    content: tuple[_ResponsesOutputTextReplay, ...] = Field(
        description="Completed visible output blocks in order."
    )
    phase: Literal["commentary", "final_answer"] | None = Field(
        default=None,
        description="Provider output phase preserved when present.",
    )


class _ResponsesFunctionCallReplay(BaseModel):
    """Retain one completed Function Tool Call for same-config replay."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["function_call"] = Field(description="Responses item discriminator.")
    id: str | None = Field(default=None, description="Optional Provider item identity.")
    call_id: str = Field(min_length=1, description="Stable Tool Call correlation identity.")
    name: str = Field(min_length=1, description="Function name validated by the Agent graph.")
    arguments: str = Field(description="Complete JSON-object arguments string.")
    status: Literal["completed"] = Field(description="Only completed calls are replayed.")

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: str) -> str:
        """Require complete object arguments even when replay targets another config."""

        _decode_arguments(value)
        return value


_ResponsesReplayItem = Annotated[
    _ResponsesReasoningReplay | _ResponsesMessageReplay | _ResponsesFunctionCallReplay,
    Field(discriminator="type"),
]


class _ResponsesReplayEnvelope(BaseModel):
    """Bind validated replay items to the exact Provider configuration that made them."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = Field(description="Replay envelope schema version.")
    api_config_id: UUID = Field(description="Provider config allowed to receive the items.")
    items: tuple[_ResponsesReplayItem, ...] = Field(
        description="Allowlisted completed Responses output items in order."
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        """Reject JSON booleans/floats that compare equal to the integer literal."""

        if type(value) is not int:
            raise ValueError("replay schema version must be an integer")
        return value


def _invalid(message: str) -> ModelGatewayError:
    """Build a contract failure using only a caller-reviewed, non-sensitive reason."""

    return ModelGatewayError("MODEL_RESPONSE_INVALID", message)


def _encode_tool_arguments(arguments: object) -> str:
    """Encode stored JSON-object arguments without coercion or non-finite numbers."""

    if not isinstance(arguments, dict):
        raise _invalid("stored model tool arguments were not a JSON object")
    try:
        return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise _invalid("stored model tool arguments were not JSON serializable") from error


def _validate_history(message: AnyMessage) -> None:
    """Reject unsupported history before either protocol serializes any fields."""

    if type(message) not in (SystemMessage, HumanMessage, AIMessage, ToolMessage):
        raise _invalid("stored history contained an unsupported message type")
    if not isinstance(message.content, str):
        raise _invalid("stored message content was not a string")
    if isinstance(message, ToolMessage) and (
        not isinstance(message.tool_call_id, str) or not message.tool_call_id
    ):
        raise _invalid("stored tool result had no correlation identity")
    if isinstance(message, AIMessage):
        if message.invalid_tool_calls:
            raise _invalid("stored assistant message contained invalid tool calls")
        seen: set[str] = set()
        for call in message.tool_calls:
            if (call.get("type") != "tool_call" or not isinstance(call.get("name"), str)
                    or not call["name"] or not isinstance(call.get("id"), str)
                    or not call["id"] or call["id"] in seen):
                raise _invalid("stored tool call had invalid or duplicate identity fields")
            seen.add(call["id"])
            _encode_tool_arguments(call.get("args"))


def _serialize_chat_messages(messages: Sequence[AnyMessage]) -> list[ChatCompletionMessageParam]:
    """Map the supported local message sequence to exact Chat request messages."""

    return [_serialize_one_chat_message(message) for message in messages]


def _serialize_one_chat_message(message: AnyMessage) -> ChatCompletionMessageParam:
    """Serialize one validated message, preserving role, content and call identity."""

    _validate_history(message)
    if isinstance(message, SystemMessage):
        if message.additional_kwargs.get("__openai_role__") == "developer":
            return {"role": "developer", "content": message.content}
        return {"role": "system", "content": message.content}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": message.content}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id}
    if isinstance(message, AIMessage):
        if message.tool_calls:
            return {"role": "assistant", "content": message.content or None, "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": _encode_tool_arguments(call["args"]),
                }} for call in message.tool_calls
            ]}
        return {"role": "assistant", "content": message.content}
    raise AssertionError("validated history type was not handled")


def _serialize_responses_input(config: ModelApiConfig, messages: Sequence[AnyMessage]) -> ResponseInputParam:
    """Map ordered local history, replaying opaque items only to their owning config."""

    result: ResponseInputParam = []
    for message in messages:
        result.extend(_serialize_one_responses_message(config, message))
    return result


def _serialize_one_responses_message(config: ModelApiConfig, message: AnyMessage) -> ResponseInputParam:
    """Validate JSON replay before config comparison; never ignore corrupt records."""

    _validate_history(message)
    if isinstance(message, AIMessage):
        if "harness_responses_replay" in message.additional_kwargs:
            try:
                # Storage uses JSON strings/arrays, so strict JSON semantics are required.
                encoded = json.dumps(message.additional_kwargs["harness_responses_replay"], allow_nan=False)
                envelope = _ResponsesReplayEnvelope.model_validate_json(encoded)
            except (ValidationError, TypeError, ValueError) as error:
                raise _invalid("stored Responses replay envelope was invalid") from error
            if envelope.api_config_id == config.api_config_id:
                return [item.model_dump(mode="json", exclude_none=True) for item in envelope.items]
        if message.tool_calls:
            return ([{"role": "assistant", "content": message.content}] if message.content else []) + [
                {"type": "function_call", "call_id": call["id"], "name": call["name"],
                 "arguments": _encode_tool_arguments(call["args"])} for call in message.tool_calls]
        return [{"role": "assistant", "content": message.content}]
    if isinstance(message, ToolMessage):
        return [{"type": "function_call_output", "call_id": message.tool_call_id, "output": message.content}]
    if isinstance(message, SystemMessage):
        if message.additional_kwargs.get("__openai_role__") == "developer":
            return [{"role": "developer", "content": message.content}]
        return [{"role": "system", "content": message.content}]
    if isinstance(message, HumanMessage):
        return [{"role": "user", "content": message.content}]
    raise AssertionError("validated history type was not handled")


def _build_chat_tools() -> list[ChatCompletionToolParam]:
    """Build the strict Chat Completions function definition."""

    definition = build_execute_command_tool_definition()
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters,
                "strict": definition.strict,
            },
        }
    ]


def _build_responses_tools() -> list[FunctionToolParam]:
    """Build the strict Responses function definition."""

    definition = build_execute_command_tool_definition()
    return [
        {
            "type": "function",
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.parameters,
            "strict": definition.strict,
        }
    ]


async def _publish_text(text: str, sink: AgentTextDeltaSink, invocation: _InvocationState) -> None:
    """Publish exact non-empty text only after checking the invocation mode."""

    if not text:
        return
    if invocation.mode is _InvocationMode.TOOL_CALL:
        raise _invalid("provider stream switched from a tool call to visible text")
    invocation.mode = _InvocationMode.FINAL_TEXT
    invocation.visible_parts.append(text)
    try:
        await sink.text_delta(text)
    except Exception as error:
        invocation.sink_error = error
        raise


def _decode_arguments(arguments: str) -> dict[str, object]:
    """Reject malformed or non-object JSON without exposing Provider arguments."""

    try:
        value = json.loads(arguments)
        _encode_tool_arguments(value)
    except (ValueError, TypeError) as error:
        raise _invalid("provider tool arguments were not a valid JSON object") from error
    if not isinstance(value, dict):
        raise _invalid("provider tool arguments were not a JSON object")
    return value


def _wire_object(value: object) -> dict[str, object]:
    """Read SDK or dictionary wire objects without validating unused metadata."""
    if isinstance(value, BaseModel):
        return value.model_dump(warnings=False)
    return value if isinstance(value, dict) else {}


def _wire_items(value: object) -> list[dict[str, object]]:
    """Skip non-object entries in optional provider collections."""
    return [_wire_object(item) for item in value] if isinstance(value, list) else []


def _wire_text(value: object) -> str:
    """Read textual payloads without coercing metadata into visible output."""
    return value if isinstance(value, str) else ""


def _provider_arguments(value: object) -> str:
    """Canonicalize object arguments, accepting the provider's literal syntax."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except ValueError:
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError) as error:
                raise _invalid("provider tool arguments could not be parsed") from error
    if value is None:
        value = {}
    return _encode_tool_arguments(value)


def _merge_argument_fragment(previous: object, incoming: object) -> object:
    """Append string deltas while accepting complete object arguments."""
    if isinstance(incoming, str):
        return _wire_text(previous) + incoming
    return incoming if isinstance(incoming, dict) else previous


def _output_slot(output: list[dict[str, object]], event: dict[str, object]) -> int:
    """Locate an item by identity first, then provider index or arrival position."""
    item = _wire_object(event.get("item"))
    identity = event.get("item_id") or item.get("id") or item.get("call_id")
    if identity:
        for index, existing in enumerate(output):
            if identity in (existing.get("id"), existing.get("call_id")):
                return index
    index = event.get("output_index")
    if type(index) is int and 0 <= index <= len(output):
        return index
    return len(output) if item else max(0, len(output) - 1)


def _update_response_output(output: list[dict[str, object]], event: dict[str, object]) -> list[dict[str, object]]:
    """Apply Responses output updates in arrival order, ignoring unrelated events."""
    kind = _wire_text(event.get("type"))
    if kind == "response.completed":
        final = _wire_items(_wire_object(event.get("response")).get("output"))
        return final or output
    if kind in ("response.output_item.added", "response.output_item.done"):
        item = _wire_object(event.get("item"))
        if item:
            index = _output_slot(output, event)
            if index < len(output):
                output[index] = dict(item)
            else:
                output.append(dict(item))
        return output
    # Metadata and hosted-tool progress never form local executable calls.
    supported = {
        "response.output_text.delta", "response.output_text.done", "response.text.delta", "response.text.done",
        "response.function_call_arguments.delta", "response.function_call_arguments.done",
        "response.content_part.added", "response.content_part.done",
        "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
    }
    if kind not in supported:
        return output
    index = _output_slot(output, event)
    if index == len(output):
        item_type = "function_call" if "function_call" in kind else "reasoning" if "reasoning" in kind else "message"
        output.append({"type": item_type, "id": event.get("item_id")})
    item = output[index]
    if "function_call_arguments" in kind:
        if kind.endswith(".delta"):
            item["arguments"] = _merge_argument_fragment(item.get("arguments"), event.get("delta"))
        elif "arguments" in event:
            item["arguments"] = event["arguments"]
        if event.get("name"):
            item["name"] = event["name"]
        return output
    summary = "reasoning_summary" in kind
    key = "summary" if summary else "content"
    content = _wire_items(item.get(key))
    position = event.get("summary_index" if summary else "content_index", 0)
    if type(position) is not int or position < 0:
        position = max(0, len(content) - 1)
    # Do not allocate arbitrary sparse arrays from provider-supplied indexes.
    position = min(position, len(content))
    if position == len(content):
        content.append({"type": "summary_text" if summary else "output_text", "text": ""})
    if "part" in event:
        part = _wire_object(event["part"])
        if part:
            content[position] = dict(part)
    elif kind.endswith(".delta"):
        content[position]["text"] = _wire_text(content[position].get("text")) + _wire_text(event.get("delta"))
    elif isinstance(event.get("text"), str):
        content[position]["text"] = event["text"]
    item[key] = content
    return output


def _local_tool_call(item: dict[str, object]) -> ToolCall:
    """Build one executable call after permissive transport aggregation."""
    name = _wire_text(item.get("name"))
    if not name:
        raise _invalid("provider tool call did not identify a function")
    identity = _wire_text(item.get("call_id") or item.get("id")) or f"call_{uuid4().hex}"
    return ToolCall(name=name, id=identity, args=_decode_arguments(_provider_arguments(item.get("arguments"))))


async def _publish_final_answer(text: str, calls: list[ToolCall], sink: AgentTextDeltaSink, invocation: _InvocationState) -> None:
    """Publish only resolved non-tool output within the existing SSE frame budget."""
    if calls:
        invocation.mode = _InvocationMode.TOOL_CALL
        return
    # Buffering permits final output replacement and mixed tool commentary.
    # Small fragments still respect the append-only Agent transport's frame cap.
    for offset in range(0, len(text), 4096):
        await _publish_text(text[offset:offset + 4096], sink, invocation)


async def _parse_chat_completions_stream(
    stream: AsyncIterator[ChatCompletionChunk], text_sink: AgentTextDeltaSink,
    invocation: _InvocationState,
) -> AIMessage:
    """Aggregate the first Chat choice, tolerating sparse metadata and finish markers."""
    text = ""
    parts: dict[object, dict[str, object]] = {}
    async for chunk in stream:
        wire = _wire_object(chunk)
        if wire.get("error"):
            raise ModelGatewayError("MODEL_REQUEST_FAILED", "provider reported an unsuccessful Chat request")
        choices = _wire_items(wire.get("choices"))
        if not choices:
            continue
        choice = choices[0]
        delta = _wire_object(choice.get("delta") or choice.get("message"))
        text += _wire_text(delta.get("content") or delta.get("refusal"))
        tools = _wire_items(delta.get("tool_calls"))
        legacy = _wire_object(delta.get("function_call"))
        if legacy:
            tools.append({"index": 0, "function": legacy})
        for position, tool in enumerate(tools):
            index = tool.get("index", position)
            if not isinstance(index, (str, int)):
                index = position
            part = parts.setdefault(index, {})
            function = _wire_object(tool.get("function"))
            if tool.get("id"):
                part["id"] = tool["id"]
            if function.get("name"):
                part["name"] = function["name"]
            if "arguments" in function:
                part["arguments"] = _merge_argument_fragment(part.get("arguments"), function["arguments"])
    calls = [_local_tool_call(part) for part in parts.values()]
    await _publish_final_answer(text, calls, text_sink, invocation)
    return AIMessage(content=text, tool_calls=calls)


def _normalize_response_item(item: dict[str, object]) -> _ResponsesReplayItem | None:
    """Project supported output to local replay without imposing wire completion fields."""
    kind = item.get("type")
    identity = _wire_text(item.get("id")) or f"item_{uuid4().hex}"
    if kind == "reasoning":
        return _ResponsesReasoningReplay(
            type="reasoning", id=identity, status="completed",
            summary=tuple(_ResponsesSummaryReplay(type="summary_text", text=_wire_text(value.get("text")))
                          for value in _wire_items(item.get("summary"))),
            encrypted_content=_wire_text(item.get("encrypted_content")) or None,
        )
    if kind == "message":
        phase = item.get("phase")
        return _ResponsesMessageReplay(
            type="message", id=identity, role="assistant", status="completed",
            phase=phase if phase in ("commentary", "final_answer") else None,
            content=tuple(_ResponsesOutputTextReplay(type="output_text", text=_wire_text(block.get("text") or block.get("refusal")))
                          for block in _wire_items(item.get("content"))
                          if block.get("type") in ("output_text", "text", "refusal")),
        )
    if kind == "function_call":
        call = _local_tool_call(item)
        return _ResponsesFunctionCallReplay(
            type="function_call", id=_wire_text(item.get("id")) or None, status="completed",
            call_id=call["id"], name=call["name"], arguments=_encode_tool_arguments(call["args"]),
        )
    # Hosted-tool/image/audio metadata is not a local execute_command request.
    return None


async def _parse_responses_stream(
    stream: AsyncIterator[ResponseStreamEvent], config: ModelApiConfig,
    text_sink: AgentTextDeltaSink, invocation: _InvocationState,
) -> AIMessage:
    """Merge Responses events like a permissive client before publishing final text."""
    output: list[dict[str, object]] = []
    async for event in stream:
        wire = _wire_object(event)
        kind = wire.get("type")
        if kind in ("error", "response.failed") or wire.get("error"):
            raise ModelGatewayError("MODEL_REQUEST_FAILED", "provider reported an unsuccessful Responses request")
        # Incomplete responses may contain usable partial output, just like Chat
        # length finishes. Normal EOF does not require a redundant terminal frame.
        if kind == "response.incomplete":
            wire = {**wire, "type": "response.completed"}
        output = _update_response_output(output, wire)
    items = tuple(normalized for item in output if (normalized := _normalize_response_item(item)) is not None)
    text = "".join(block.text for item in items if isinstance(item, _ResponsesMessageReplay) for block in item.content)
    calls = [ToolCall(name=item.name, id=item.call_id, args=_decode_arguments(item.arguments))
             for item in items if isinstance(item, _ResponsesFunctionCallReplay)]
    envelope = _ResponsesReplayEnvelope(schema_version=1, api_config_id=config.api_config_id, items=items)
    await _publish_final_answer(text, calls, text_sink, invocation)
    return AIMessage(content=text, tool_calls=calls, additional_kwargs={
        "harness_responses_replay": envelope.model_dump(mode="json", exclude_none=True),
    })


@asynccontextmanager
async def _close_on_exit(
    resource: AsyncOpenAI | AsyncStream[ChatCompletionChunk] | AsyncStream[ResponseStreamEvent],
) -> AsyncIterator[None]:
    """Close an owned SDK resource; cleanup must never replace an earlier failure."""

    earlier: BaseException | None = None
    try:
        yield
    except BaseException as error:
        earlier = error
        raise
    finally:
        try:
            await resource.close()
        except BaseException:
            # A successful operation must expose cleanup failure. On failure/cancel,
            # retain the original exception rather than masking the real cause.
            if earlier is None:
                raise


async def _invoke_chat_completions(
    client: AsyncOpenAI, config: ModelApiConfig, messages: Sequence[AnyMessage],
    sink: AgentTextDeltaSink, invocation: _InvocationState,
) -> AIMessage:
    """Own a fresh Chat stream for this attempt with an explicit request mapping."""

    stream = await client.chat.completions.create(
        model=config.model, messages=_serialize_chat_messages(messages),
        tools=_build_chat_tools(), parallel_tool_calls=False, stream=True,
    )


    async with _close_on_exit(stream):
        await stream.__aenter__()
        return await _parse_chat_completions_stream(stream, sink, invocation)


async def _invoke_responses(
    client: AsyncOpenAI, config: ModelApiConfig, messages: Sequence[AnyMessage],
    sink: AgentTextDeltaSink, invocation: _InvocationState,
) -> AIMessage:
    """Own a fresh Responses stream using local history as the input authority."""

    stream = await client.responses.create(
        model=config.model, input=_serialize_responses_input(config, messages),
        tools=_build_responses_tools(), parallel_tool_calls=False,
        include=["reasoning.encrypted_content"], stream=True,
    )
    async with _close_on_exit(stream):
        await stream.__aenter__()
        return await _parse_responses_stream(stream, config, sink, invocation)
