"""显式双 API 模型构造与仅针对超时的重试策略。"""

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
    """跟踪一次 Provider 调用属于工具调用还是最终文本。"""

    UNDECIDED = "UNDECIDED"
    TOOL_CALL = "TOOL_CALL"
    FINAL_TEXT = "FINAL_TEXT"


@dataclass(slots=True)
class _InvocationState:
    """保留每次尝试的语义模式和已发布的精确可见片段。"""

    # 聚合完整 Provider 调用后确定的语义选择。
    mode: _InvocationMode = _InvocationMode.UNDECIDED
    # 仅在本次 Provider 尝试中发布的精确最终文本片段。
    visible_parts: list[str] = field(default_factory=list)
    # 保留原始发布端失败，仅用于避免错误归因。
    sink_error: Exception | None = None


class ModelGatewayError(RuntimeError):
    """携带模型终态失败的稳定非敏感错误码。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存公开错误码与已审查的非敏感失败原因。"""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # 稳定的 Run 失败错误码。
        self.safe_message = message  # 经过审查且不含 Provider 内容的详情。


class ModelGateway:
    """每次调用独占一个官方 SDK 客户端，负责重试与取消。"""

    def __init__(
            self,
            *,
            client_builder: OpenAIClientBuilder = AsyncOpenAI,
            sleep: Sleep = asyncio.sleep,
    ) -> None:
        """绑定官方客户端构造器和可注入的重试等待函数。"""

        self._client_builder = client_builder  # 创建由本次调用独占的 SDK 客户端。
        self._sleep = sleep  # 退避等待仍可由调用方取消。

    async def invoke(
            self,
            config: ModelApiConfig,
            api_key: SecretStr,
            messages: Sequence[AnyMessage],
            cancelled: asyncio.Event,
            text_sink: AgentTextDeltaSink,
    ) -> AIMessage:
        """流式发布最终可见文本，同时返回完整 AIMessage。"""

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

    async def summarize_once(
        self, config: ModelApiConfig, api_key: SecretStr,
        messages: Sequence[AnyMessage], cancelled: asyncio.Event,
    ) -> str:
        """独占且仅发起一次无工具摘要请求，不发布可见文本。"""
        # 1. 请求前检查取消，为本次摘要创建禁用 SDK 重试的独立客户端。
        if cancelled.is_set():
            raise AgentCancelled()
        client = self._client_builder(api_key=api_key.get_secret_value(),
            base_url=config.base_url, timeout=MODEL_REQUEST_TIMEOUT_SECONDS, max_retries=0)
        # 2. 按本轮 API 类型调用无工具的摘要入口，整体截止时间和取消共同约束请求。
        try:
            async with _close_on_exit(client):
                invoke = _invoke_responses if config.api_type.value == "RESPONSES" else _invoke_chat_completions
                # SDK socket 超时在收到进展后会重置；此截止时间约束
                # 整条摘要流，包括缓慢但永不结束的流。
                async with asyncio.timeout(MODEL_REQUEST_TIMEOUT_SECONDS):
                    message = await _await_with_cancellation(
                        invoke(client, config, messages, _SummarySink(), _InvocationState(), summary=True), cancelled)
                # 3. 返回完整摘要正文；内部 sink 不连接 UI，客户端由外层上下文负责关闭。
                return message.content
        # 4. 取消和已知错误原样传播；其他请求异常转为安全错误，由 compactor 决定重试。
        except (AgentCancelled, ModelGatewayError, asyncio.CancelledError):
            raise
        except Exception as error:
            raise ModelGatewayError("MODEL_REQUEST_FAILED", "the summary Provider request failed") from error

    async def _invoke_attempts(
        self, config: ModelApiConfig, client: AsyncOpenAI,
        messages: Sequence[AnyMessage], cancelled: asyncio.Event,
        text_sink: AgentTextDeltaSink,
    ) -> AIMessage:
        """只重试尚未发布可见文本时的超时，复用本次调用的连接池。"""

        # 1. 每次尝试重建聚合状态，未发布的草稿不跨尝试复用。
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
                # 流自身的失败保留原错误码，便于 AgentService
                # 持久化 Run 终态，避免错误归因到 Provider。
                # 2. 先辨别发布器失败与 Provider 超时；非超时失败直接终止。
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
                # 3. 仅在尚未进入最终文本模式且次数未耗尽时执行可取消退避。
                await _await_with_cancellation(
                    self._sleep(MODEL_RETRY_DELAYS_SECONDS[attempt]),
                    cancelled,
                )
                continue
            # 4. 成功结果必须为完整 AIMessage，禁止把无效返回值当作回答。
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
        """仅派发配置指定的 API，不探测或切换协议重试。"""

        if config.api_type is ApiType.RESPONSES:
            return await _invoke_responses(client, config, messages, text_sink, invocation)
        if config.api_type is ApiType.CHAT_COMPLETIONS:
            return await _invoke_chat_completions(client, config, messages, text_sink, invocation)
        raise AssertionError("unsupported validated API type")


def _safe_provider_error_fields(error: BaseException) -> dict[str, object]:
    """提取允许的 Provider 元数据，不包含异常或响应文本。"""

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
    """让操作与取消竞争，并等待所有已取消任务结束。"""

    if cancelled.is_set():
        if asyncio.iscoroutine(operation):
            operation.close()
        raise AgentCancelled(
            message="the async operation was cancelled before it started"
        )
    # 1. 同时拥有业务任务与取消等待任务，避免取消后遗留后台操作。
    operation_task = asyncio.ensure_future(operation)
    cancel_task = asyncio.create_task(cancelled.wait())
    try:
        # 2. 等待任一任务先完成；取消获胜时取消并等待业务任务。
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
        # 外层 Task 归 dispatcher 所有，不得遗留 Provider 或退避任务。
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise
    # 3. 退出时回收取消等待任务，调用方取消继续向上传播。
    finally:
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)


def _is_network_timeout(error: BaseException) -> bool:
    """沿显式原因链仅识别具有确定类型的超时异常。"""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (openai.APITimeoutError, asyncio.TimeoutError)):
            return True
        current = current.__cause__
    return False


class _ResponsesSummaryReplay(BaseModel):
    """保留模型生成的推理摘要项，仅供同一配置回放。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["summary_text"] = Field(description="Responses summary discriminator.")
    text: str = Field(description="Model-generated summary returned by the Provider.")


class _ResponsesReasoningReplay(BaseModel):
    """保留无状态工具续接所需的已完成推理项。"""

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
    """保留已完成的 Responses 可见文本块，不带原始元数据。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["output_text"] = Field(description="Responses text discriminator.")
    text: str = Field(description="Exact final visible text.")
    annotations: tuple[()] = Field(
        default=(),
        description="Annotations are intentionally not replayed by this Agent.",
    )


class _ResponsesMessageReplay(BaseModel):
    """保留已完成的 assistant 消息及其可选阶段。"""

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
    """保留已完成的函数工具调用，仅供同一配置回放。"""

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
        """即使回放目标为其他配置，也要求完整的对象参数。"""

        _decode_arguments(value)
        return value


_ResponsesReplayItem = Annotated[
    _ResponsesReasoningReplay | _ResponsesMessageReplay | _ResponsesFunctionCallReplay,
    Field(discriminator="type"),
]


class _ResponsesReplayEnvelope(BaseModel):
    """将已校验回放项绑定到生成它们的精确 Provider 配置。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = Field(description="Replay envelope schema version.")
    api_config_id: UUID = Field(description="Provider config allowed to receive the items.")
    items: tuple[_ResponsesReplayItem, ...] = Field(
        description="Allowlisted completed Responses output items in order."
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        """拒绝与整数字面值比较相等的 JSON 布尔值和浮点数。"""

        if type(value) is not int:
            raise ValueError("replay schema version must be an integer")
        return value


def _invalid(message: str) -> ModelGatewayError:
    """仅使用调用方已审查的非敏感原因构建契约失败。"""

    return ModelGatewayError("MODEL_RESPONSE_INVALID", message)


def _encode_tool_arguments(arguments: object) -> str:
    """编码已保存的 JSON 对象参数，拒绝强制转换和非有限数。"""

    if not isinstance(arguments, dict):
        raise _invalid("stored model tool arguments were not a JSON object")
    try:
        return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise _invalid("stored model tool arguments were not JSON serializable") from error


def _validate_history(message: AnyMessage) -> None:
    """在任一协议序列化字段前拒绝不支持的历史。"""

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
    """将支持的本地消息序列精确映射为 Chat 请求消息。"""

    return [_serialize_one_chat_message(message) for message in messages]


def _serialize_one_chat_message(message: AnyMessage) -> ChatCompletionMessageParam:
    """序列化一条已校验消息，保留角色、内容和调用标识。"""

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
    """映射有序本地历史，仅向所属配置回放不透明项。"""

    result: ResponseInputParam = []
    for message in messages:
        result.extend(_serialize_one_responses_message(config, message))
    return result


def _serialize_one_responses_message(config: ModelApiConfig, message: AnyMessage) -> ResponseInputParam:
    """先校验 JSON 回放数据再比较配置，绝不忽略损坏记录。"""

    _validate_history(message)
    if isinstance(message, AIMessage):
        if "harness_responses_replay" in message.additional_kwargs:
            try:
                # 存储使用 JSON 字符串和数组，因此必须遵循严格 JSON 语义。
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
    """构建严格的 Chat Completions 函数定义。"""

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
    """构建严格的 Responses 函数定义。"""

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
    """检查调用模式后才发布精确的非空文本。"""

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
    """拒绝格式错误或非对象 JSON，不暴露 Provider 参数。"""

    try:
        value = json.loads(arguments)
        _encode_tool_arguments(value)
    except (ValueError, TypeError) as error:
        raise _invalid("provider tool arguments were not a valid JSON object") from error
    if not isinstance(value, dict):
        raise _invalid("provider tool arguments were not a JSON object")
    return value


def _wire_object(value: object) -> dict[str, object]:
    """读取 SDK 或字典形式的线上对象，不校验未使用的元数据。"""
    if isinstance(value, BaseModel):
        return value.model_dump(warnings=False)
    return value if isinstance(value, dict) else {}


def _wire_items(value: object) -> list[dict[str, object]]:
    """跳过 Provider 可选集合中的非对象项。"""
    return [_wire_object(item) for item in value] if isinstance(value, list) else []


def _wire_text(value: object) -> str:
    """读取文本载荷，不把元数据强制转换为可见输出。"""
    return value if isinstance(value, str) else ""


def _provider_arguments(value: object) -> str:
    """规范化对象参数，允许 Provider 使用字面量语法。"""
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
    """追加字符串增量，同时接受完整对象参数。"""
    if isinstance(incoming, str):
        return _wire_text(previous) + incoming
    return incoming if isinstance(incoming, dict) else previous


def _output_slot(output: list[dict[str, object]], event: dict[str, object]) -> int:
    """优先按标识定位项，其次使用 Provider 下标或到达位置。"""
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
    """按到达顺序应用 Responses 输出更新，忽略无关事件。"""
    # 1. 优先处理完整响应和整项更新，最终非空 output 可替换累计结果。
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
    # 元数据和托管工具进展不能构成本地可执行调用。
    # 2. 只聚合受支持的文本、推理摘要和函数参数事件。
    supported = {
        "response.output_text.delta", "response.output_text.done", "response.text.delta", "response.text.done",
        "response.function_call_arguments.delta", "response.function_call_arguments.done",
        "response.content_part.added", "response.content_part.done",
        "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
    }
    if kind not in supported:
        return output
    # 3. 按项标识优先定位输出位置，再分别合并工具参数或内容块。
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
    # 4. 分开保存推理摘要与可见正文，并限制内容下标的分配范围。
    summary = "reasoning_summary" in kind
    key = "summary" if summary else "content"
    content = _wire_items(item.get(key))
    position = event.get("summary_index" if summary else "content_index", 0)
    if type(position) is not int or position < 0:
        position = max(0, len(content) - 1)
    # 不得依据 Provider 提供的下标分配任意大小的稀疏数组。
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
    """宽松聚合传输数据后构建可执行调用。"""
    name = _wire_text(item.get("name"))
    if not name:
        raise _invalid("provider tool call did not identify a function")
    identity = _wire_text(item.get("call_id") or item.get("id")) or f"call_{uuid4().hex}"
    return ToolCall(name=name, id=identity, args=_decode_arguments(_provider_arguments(item.get("arguments"))))


async def _publish_final_answer(text: str, calls: list[ToolCall], sink: AgentTextDeltaSink, invocation: _InvocationState) -> None:
    """在现有 SSE 帧预算内只发布已确定的非工具输出。"""
    if calls:
        invocation.mode = _InvocationMode.TOOL_CALL
        return
    # 缓冲允许最终输出替换及混合工具说明文本。
    # 分片仍须遵守只追加 Agent 传输的单帧上限。
    for offset in range(0, len(text), 4096):
        await _publish_text(text[offset:offset + 4096], sink, invocation)


async def _parse_chat_completions_stream(
    stream: AsyncIterator[ChatCompletionChunk], text_sink: AgentTextDeltaSink,
    invocation: _InvocationState, *, require_complete: bool = False,
) -> AIMessage:
    """聚合首个 Chat choice，容忍稀疏元数据与结束标记。"""
    # 1. 为本次尝试独立缓冲文本、usage 和工具参数，先不发布可见片段。
    text = ""
    usage = None
    finish_reason = None
    parts: dict[object, dict[str, object]] = {}
    # 2. 按到达顺序消费首个 choice；usage 独立读取，明确 Provider 错误立即失败。
    async for chunk in stream:
        wire = _wire_object(chunk)
        if wire.get("error"):
            raise ModelGatewayError("MODEL_REQUEST_FAILED", "provider reported an unsuccessful Chat request")
        candidate_usage = _normalized_usage(wire.get("usage"), chat=True)
        if candidate_usage is not None:
            usage = candidate_usage
        choices = _wire_items(wire.get("choices"))
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
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
    # 3. 流结束后校验本地工具参数；摘要另要求完整结束且不含工具。
    calls = [_local_tool_call(part) for part in parts.values()]
    if require_complete and (finish_reason != "stop" or calls or not text.strip()):
        raise _invalid("the summary was empty, incomplete, or contained tool calls")
    # 4. 仅最终纯文本轮向 sink 发布，再返回完整消息供持久化。
    await _publish_final_answer(text, calls, text_sink, invocation)
    return AIMessage(content=text, tool_calls=calls, usage_metadata=usage)


def _normalize_response_item(item: dict[str, object]) -> _ResponsesReplayItem | None:
    """将支持的输出投影为本地回放数据，不强加线上完成字段。"""
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
    # 托管工具、图片或音频元数据不是本地 execute_command 请求。
    return None


async def _parse_responses_stream(
    stream: AsyncIterator[ResponseStreamEvent], config: ModelApiConfig,
    text_sink: AgentTextDeltaSink, invocation: _InvocationState, *, require_complete: bool = False,
) -> AIMessage:
    """宽松合并 Responses 事件后发布最终文本。"""
    # 1. 独立初始化输出聚合状态，保留本次调用的 usage 和完成状态。
    output: list[dict[str, object]] = []
    usage = None
    completed = False
    # 2. 按到达顺序聚合线上事件，忽略未消费元数据，明确错误直接传播。
    async for event in stream:
        wire = _wire_object(event)
        kind = wire.get("type")
        response = _wire_object(wire.get("response"))
        candidate_usage = _normalized_usage(response.get("usage"), chat=False)
        if candidate_usage is not None:
            usage = candidate_usage
        if kind in ("response.completed", "response.incomplete"):
            completed = kind == "response.completed" and response.get("status") == "completed"
        if kind in ("error", "response.failed") or wire.get("error"):
            raise ModelGatewayError("MODEL_REQUEST_FAILED", "provider reported an unsuccessful Responses request")
        # 未完整完成的响应可能带有可用的部分输出，类似 Chat 因长度限制结束；
        # 正常 EOF 不要求额外的冗余终止帧。
        if kind == "response.incomplete":
            wire = {**wire, "type": "response.completed"}
        output = _update_response_output(output, wire)
    # 3. 将支持的项规范化为本地回放数据，再提取可见文本与可执行调用。
    items = tuple(normalized for item in output if (normalized := _normalize_response_item(item)) is not None)
    text = "".join(block.text for item in items if isinstance(item, _ResponsesMessageReplay) for block in item.content)
    calls = [ToolCall(name=item.name, id=item.call_id, args=_decode_arguments(item.arguments))
             for item in items if isinstance(item, _ResponsesFunctionCallReplay)]
    if require_complete and (not completed or calls or not text.strip()):
        raise _invalid("the summary was empty, incomplete, or contained tool calls")
    # 4. 绑定回放所属配置；通过摘要完整性校验后才发布最终答案。
    envelope = _ResponsesReplayEnvelope(schema_version=1, api_config_id=config.api_config_id, items=items)
    await _publish_final_answer(text, calls, text_sink, invocation)
    return AIMessage(content=text, tool_calls=calls, usage_metadata=usage, additional_kwargs={
        "harness_responses_replay": envelope.model_dump(mode="json", exclude_none=True),
    })


@asynccontextmanager
async def _close_on_exit(
    resource: AsyncOpenAI | AsyncStream[ChatCompletionChunk] | AsyncStream[ResponseStreamEvent],
) -> AsyncIterator[None]:
    """关闭拥有的 SDK 资源，清理失败不得覆盖更早的失败。"""

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
            # 操作成功时必须暴露清理失败；操作失败或取消时，
            # 保留原始异常，避免掩盖真正原因。
            if earlier is None:
                raise


async def _invoke_chat_completions(
    client: AsyncOpenAI, config: ModelApiConfig, messages: Sequence[AnyMessage],
    sink: AgentTextDeltaSink, invocation: _InvocationState, *, summary: bool = False,
) -> AIMessage:
    """本次尝试独占新的 Chat 流，并使用显式请求映射。"""

    stream = await client.chat.completions.create(
        model=config.model, **model_input_payload(config, messages, include_tools=not summary),
        max_completion_tokens=config.max_output_tokens,
        stream_options={"include_usage": True}, stream=True,
    )


    async with _close_on_exit(stream):
        await stream.__aenter__()
        return await _parse_chat_completions_stream(stream, sink, invocation, require_complete=summary)


async def _invoke_responses(
    client: AsyncOpenAI, config: ModelApiConfig, messages: Sequence[AnyMessage],
    sink: AgentTextDeltaSink, invocation: _InvocationState, *, summary: bool = False,
) -> AIMessage:
    """以本地历史为输入权威，独占新的 Responses 流。"""

    stream = await client.responses.create(
        model=config.model, **model_input_payload(config, messages, include_tools=not summary),
        max_output_tokens=config.max_output_tokens,
        include=["reasoning.encrypted_content"], stream=True,
    )
    async with _close_on_exit(stream):
        await stream.__aenter__()
        return await _parse_responses_stream(stream, config, sink, invocation, require_complete=summary)


class _SummarySink:
    """丢弃内部摘要发布，不访问 UI sink。"""
    async def text_delta(self, delta: str) -> None:
        """仅接收已校验的内部文本；完整值由调用方返回。"""


def _normalized_usage(value: object, *, chat: bool) -> dict[str, int] | None:
    """只接受完整非负 usage，不补造缺失计数。"""
    data = _wire_object(value)
    incoming = data.get("prompt_tokens" if chat else "input_tokens")
    outgoing = data.get("completion_tokens" if chat else "output_tokens")
    if type(incoming) is not int or type(outgoing) is not int or incoming <= 0 or outgoing < 0:
        return None
    return {"input_tokens": incoming, "output_tokens": outgoing, "total_tokens": incoming + outgoing}


def model_input_payload(config: ModelApiConfig, messages: Sequence[AnyMessage],
                        *, include_tools: bool) -> dict[str, object]:
    """让计数与请求共用精确的协议输入投影。"""
    if config.api_type.value == "RESPONSES":
        payload = {"input": _serialize_responses_input(config, messages)}
        tools = _build_responses_tools()
    else:
        payload = {"messages": _serialize_chat_messages(messages)}
        tools = _build_chat_tools()
    if include_tools:
        payload.update(tools=tools, parallel_tool_calls=False)
    return payload
