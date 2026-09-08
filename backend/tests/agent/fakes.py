"""Agent 图和网关测试共享的 typed 确定性替身。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import cast
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import ResponseStreamEvent
from pydantic import TypeAdapter

from harness_shell_sidecar.agent.contracts import AgentRun, AgentTurnInput, ExecuteCommandArguments

_RESPONSE_EVENT_ADAPTER = TypeAdapter(ResponseStreamEvent)


class RecordingTurnSink:
    """记录测试轮次的生命周期事件和精确可见增量。"""

    def __init__(self) -> None:
        """创建空事件时间线和流式文本缓冲区。"""

        self.events: list[tuple[str, AgentRun | str | dict[str, object]]] = []
        self.parts: list[str] = []
        self.failure_messages: list[str] = []
        self.approval_ready = asyncio.Event()
        self.approval_requests = []

    @property
    def streamed_text(self) -> str:
        """按到达顺序拼接精确可见增量。"""

        return "".join(self.parts)

    async def started(self, run: AgentRun) -> None:
        """记录持久化 RUNNING 快照。"""

        self.events.append(("started", run))

    async def tool_started(self, tool_call_id: str, arguments: ExecuteCommandArguments) -> None:
        """记录工具状态，以便验证实际执行前的事件顺序。"""
        self.events.append(("tool_started", {"tool_call_id": tool_call_id, "tool_name": "execute_command", "arguments": arguments.model_dump()}))

    async def text_delta(self, delta: str) -> None:
        """记录精确可见增量。"""

        self.parts.append(delta)
        self.events.append(("delta", delta))

    async def text_replace(self, text: str) -> None:
        """记录完整文本更新及其发生顺序。"""
        self.parts[:] = [text]
        self.events.append(("replace", text))

    async def approval_requested(self, request) -> None:
        """保存完整测试审核并唤醒测试调用方。"""
        self.approval_requests.append(request)
        self.events.append(("approval_requested", request))
        self.approval_ready.set()

    async def approval_resolved(self, resolution, tool_call_id: str) -> None:
        """记录审核终态。"""
        self.events.append(("approval_resolved", resolution))

    async def completed(self, run: AgentRun) -> None:
        """记录持久化成功终态快照。"""

        self.events.append(("completed", run))

    async def failed(self, run: AgentRun, message: str) -> None:
        """记录持久化终态快照及已审查公开消息。"""

        self.events.append(("failed", run))
        self.failure_messages.append(message)


async def instant_sleep(_: float) -> None:
    """在确定性测试中立即完成重试延时。"""


def make_tool_call(call_id: str, command: str) -> dict[str, object]:
    """构建规范 LangChain execute_command 调用。"""

    return {
        "name": "execute_command",
        "args": {"command": command},
        "id": call_id,
        "type": "tool_call",
    }


def make_turn_input(*, conversation_id: UUID | None = None) -> AgentTurnInput:
    """使用新不透明 ID 构建合法图服务输入。"""

    return AgentTurnInput(
        conversation_id=conversation_id,
        ssh_session_id=uuid4(),
        api_config_id=uuid4(),
        user_message="inspect the remote host",
    )


class FakeAsyncStream:
    """产出确定性事件序列并记录确定性关闭。"""

    def __init__(self, events: Sequence[object]) -> None:
        """为单个 SDK 请求复制事件序列。"""

        self._events = list(events)
        self.closed = False

    async def __aenter__(self) -> FakeAsyncStream:
        """将此流作为请求拥有的上下文管理器返回。"""

        return self

    async def __aexit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object | None,
    ) -> None:
        """在成功、失败或取消时关闭流。"""

        await self.close()

    def __aiter__(self) -> AsyncIterator[object]:
        """返回遍历队列事件的新异步迭代器。"""

        return self._iterate()

    async def _iterate(self) -> AsyncIterator[object]:
        """按序产出事件或抛出排队的异常。"""

        for event in self._events:
            if isinstance(event, asyncio.Event):
                await event.wait()
                continue
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self) -> None:
        """记录幂等流关闭。"""

        self.closed = True


class RecordingSDKResource:
    """返回排队的流，同时记录精确官方 SDK 关键字参数。"""

    def __init__(self, outcomes: Sequence[Sequence[object]]) -> None:
        """为每次预期 create 调用复制事件列表。"""

        self._outcomes = [list(outcome) for outcome in outcomes]
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []
        self.streams: list[FakeAsyncStream] = []

    async def create(self, **kwargs: object) -> FakeAsyncStream:
        """返回下一个流，不联系 Provider。"""

        self.calls += 1
        self.kwargs.append(dict(kwargs))
        if not self._outcomes:
            raise AssertionError("RecordingSDKResource outcome queue is empty")
        stream = FakeAsyncStream(self._outcomes.pop(0))
        self.streams.append(stream)
        return stream


class _FakeChatNamespace:
    """暴露官方 client.chat.completions 资源路径。"""

    def __init__(self, outcomes: Sequence[Sequence[object]]) -> None:
        """创建记录调用的 Chat Completions 资源。"""

        self.completions = RecordingSDKResource(outcomes)


class FakeOpenAIClient:
    """无需网络即可暴露独立 Chat 和 Responses 资源。"""

    def __init__(
        self,
        *,
        chat_outcomes: Sequence[Sequence[object]],
        responses_outcomes: Sequence[Sequence[object]],
    ) -> None:
        """创建独立资源队列和可观察关闭标志。"""

        self.chat = _FakeChatNamespace(chat_outcomes)
        self.responses = RecordingSDKResource(responses_outcomes)
        self.closed = False

    async def close(self) -> None:
        """记录确定性客户端清理。"""

        self.closed = True


class RecordingOpenAIClientBuilder:
    """返回排队的客户端替身，记录精确 AsyncOpenAI 构造参数。"""

    def __init__(self, clients: Sequence[FakeOpenAIClient]) -> None:
        """为独立网关调用复制预期客户端序列。"""

        self._clients = list(clients)
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> AsyncOpenAI:
        """通过生产构造器类型返回下一个客户端。"""

        self.calls += 1
        self.kwargs.append(dict(kwargs))
        if not self._clients:
            raise AssertionError("OpenAI client queue is empty")
        return cast(AsyncOpenAI, self._clients.pop(0))


def chat_chunk(*, content: str | None = None, finish_reason: str | None = None, tool_calls: list[dict[str, object]] | None = None) -> ChatCompletionChunk:
    """构建具体 SDK Chat 分块，包含全部必需传输字段。"""
    from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, Choice, ChoiceDelta
    return ChatCompletionChunk(id="chat-test", choices=[Choice(index=0, delta=ChoiceDelta(content=content, tool_calls=tool_calls), finish_reason=finish_reason)], created=0, model="test-model", object="chat.completion.chunk")


def response_event(kind: str, sequence: int, **fields: object) -> ResponseStreamEvent:
    """通过已安装官方 SDK 事件联合类型校验完整 fixture。"""
    return _RESPONSE_EVENT_ADAPTER.validate_python({"type": kind, "sequence_number": sequence, **fields})


def response_body(output: list[dict[str, object]], status: str = "completed") -> dict[str, object]:
    """无需真实 Provider 即提供官方响应的必需字段。"""
    return dict(id="resp-test", created_at=0, model="test-model", object="response", output=output, status=status, parallel_tool_calls=False, tool_choice="auto", tools=[])


def responses_events(message: AIMessage) -> list[object]:
    """将图级回答转换为完整 typed Responses 输出事件。"""
    events: list[object] = []
    output: list[dict[str, object]] = []
    if message.tool_calls:
        for index, call in enumerate(message.tool_calls):
            import json
            arguments = json.dumps(call["args"], separators=(",", ":"))
            item = dict(type="function_call", id=f"fc-{index}", call_id=call["id"], name=call["name"], arguments=arguments, status="completed")
            events.append(response_event("response.function_call_arguments.delta", len(events), item_id=item["id"], output_index=index, delta=arguments))
            events.append(response_event("response.function_call_arguments.done", len(events), item_id=item["id"], output_index=index, name=call["name"], arguments=arguments))
            events.append(response_event("response.output_item.done", len(events), output_index=index, item=item))
            output.append(item)
    else:
        text = message.content
        events.append(response_event("response.output_text.delta", len(events), item_id="msg-0", output_index=0, content_index=0, delta=text, logprobs=[]))
        item = dict(type="message", id="msg-0", role="assistant", status="completed", content=[dict(type="output_text", text=text, annotations=[], logprobs=[])])
        events.append(response_event("response.output_text.done", len(events), item_id="msg-0", output_index=0, content_index=0, text=text, logprobs=[]))
        events.append(response_event("response.output_item.done", len(events), output_index=0, item=item))
        output.append(item)
    events.append(response_event("response.completed", len(events), response=response_body(output)))
    return events


class FakeModelSequence:
    """将图回答排队；每个 SDK 资源请求消费一个回答。"""

    def __init__(self, outcomes: Sequence[object] | None = None) -> None:
        """保留排队结果和精确序列化 SDK 输入历史。"""
        self.outcomes = list(outcomes or [])
        self.calls = 0
        self.message_calls: list[list[dict[str, object]]] = []
        self.streams: list[FakeAsyncStream] = []

    def queue(self, *outcomes: object) -> None:
        """追加回答，不替换此前历史。"""
        self.outcomes.extend(outcomes)

    async def create_stream(self, responses: bool, **kwargs: object) -> FakeAsyncStream:
        """将排队回答转换为所选协议的具体 SDK 事件。"""
        self.calls += 1
        self.message_calls.append(list(kwargs["input" if responses else "messages"]))
        if not self.outcomes:
            raise AssertionError("model sequence outcome queue is empty")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, AIMessage):
            events = responses_events(outcome) if responses else chat_events(outcome)
        elif isinstance(outcome, asyncio.Event):
            events = [outcome, *(responses_events(AIMessage(content="released")) if responses else chat_events(AIMessage(content="released")))]
        elif isinstance(outcome, list):
            events = outcome
        else:
            events = [outcome]
        stream = FakeAsyncStream(events)
        self.streams.append(stream)
        return stream


class _SequenceResource:
    """暴露由图结果队列支持的具体 SDK 资源。"""

    def __init__(self, sequence: FakeModelSequence, responses: bool) -> None:
        """绑定协议，不从请求内容猜测。"""
        self.sequence = sequence
        self.responses = responses

    async def create(self, **kwargs: object) -> FakeAsyncStream:
        """在 SDK 边界记录并消费图回答。"""
        return await self.sequence.create_stream(self.responses, **kwargs)


class RecordingSequenceClientBuilder:
    """每次调用创建新的 SDK 替身，同时共享图结果。"""

    def __init__(self, sequence: FakeModelSequence) -> None:
        """与此前图 fixture 一样，隐藏诊断中的凭据。"""
        self.sequence = sequence
        self.calls = 0
        self.kwargs: dict[str, object] = {}
        self.clients: list[FakeOpenAIClient] = []

    def __call__(self, **kwargs: object) -> AsyncOpenAI:
        """记录构造器元数据并注入选定资源队列。"""
        from pydantic import SecretStr
        self.calls += 1
        self.kwargs = {**kwargs, "api_key": SecretStr(kwargs["api_key"])}
        client = FakeOpenAIClient(chat_outcomes=[], responses_outcomes=[])
        client.chat.completions = _SequenceResource(self.sequence, False)
        client.responses = _SequenceResource(self.sequence, True)
        self.clients.append(client)
        return cast(AsyncOpenAI, client)


class CancellationAwareModel(FakeModelSequence):
    """向服务测试暴露取消到达活动 SDK 迭代器的状态。"""

    def __init__(self) -> None:
        """为阻塞调用创建可观察生命周期门禁。"""
        super().__init__()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.release = asyncio.Event()

    async def create_stream(self, responses: bool, **kwargs: object) -> FakeAsyncStream:
        """返回在迭代时而非创建时阻塞的流。"""
        owner = self
        class BlockingStream(FakeAsyncStream):
            """阻塞至释放或取消，并记录确定性结束。"""
            async def _iterate(self) -> AsyncIterator[object]:
                """向所属 fixture 暴露活动迭代生命周期。"""
                owner.started.set()
                try:
                    await owner.release.wait()
                    for event in (responses_events(AIMessage(content="released")) if responses else chat_events(AIMessage(content="released"))):
                        yield event
                finally:
                    owner.stopped.set()
        self.calls += 1
        stream = BlockingStream([])
        self.streams.append(stream)
        return stream


def chat_events(message: AIMessage) -> list[object]:
    """将规范图回答转换为完整 Chat completion 事件。"""
    import json
    if message.tool_calls:
        return [chat_chunk(tool_calls=[{"index": index, "id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"], separators=(",", ":"))}} for index, call in enumerate(message.tool_calls)]), chat_chunk(finish_reason="tool_calls")]
    return [chat_chunk(content=message.content), chat_chunk(finish_reason="stop")]


class FakeSessionRegistry:
    """为非 SSH 集成测试提供固定目标和不会自行断连的借用会话。"""

    def get(self, session_id):
        """返回仅用于目标展示的非秘密快照。"""
        from types import SimpleNamespace
        return SimpleNamespace(host_label="test", host="localhost", port=22, username="tester")

    async def wait_unavailable(self, session_id) -> None:
        """等待测试取消，明确模拟始终连接的会话。"""
        await asyncio.Event().wait()
