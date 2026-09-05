"""Typed deterministic fakes shared by Agent graph and gateway tests."""

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

from harness_shell_sidecar.agent.contracts import AgentRun, AgentTurnInput

_RESPONSE_EVENT_ADAPTER = TypeAdapter(ResponseStreamEvent)


class RecordingTurnSink:
    """Record lifecycle events and exact visible deltas for one test turn."""

    def __init__(self) -> None:
        """Create an empty event timeline and streamed-text buffer."""

        self.events: list[tuple[str, AgentRun | str]] = []
        self.parts: list[str] = []
        self.failure_messages: list[str] = []

    @property
    def streamed_text(self) -> str:
        """Join the exact visible deltas in arrival order."""

        return "".join(self.parts)

    async def started(self, run: AgentRun) -> None:
        """Record the durable RUNNING snapshot."""

        self.events.append(("started", run))

    async def text_delta(self, delta: str) -> None:
        """Record one exact visible delta."""

        self.parts.append(delta)
        self.events.append(("delta", delta))

    async def completed(self, run: AgentRun) -> None:
        """Record the durable successful terminal snapshot."""

        self.events.append(("completed", run))

    async def failed(self, run: AgentRun, message: str) -> None:
        """Record the durable terminal snapshot and its reviewed public message."""

        self.events.append(("failed", run))
        self.failure_messages.append(message)


async def instant_sleep(_: float) -> None:
    """Complete retry delays immediately in deterministic tests."""


def make_tool_call(call_id: str, command: str) -> dict[str, object]:
    """Build one canonical LangChain execute_command call."""

    return {
        "name": "execute_command",
        "args": {"command": command},
        "id": call_id,
        "type": "tool_call",
    }


def make_turn_input(*, conversation_id: UUID | None = None) -> AgentTurnInput:
    """Build a valid graph-service input with fresh opaque IDs."""

    return AgentTurnInput(
        conversation_id=conversation_id,
        ssh_session_id=uuid4(),
        api_config_id=uuid4(),
        user_message="inspect the remote host",
    )


class FakeAsyncStream:
    """Yield one deterministic event sequence and record deterministic closure."""

    def __init__(self, events: Sequence[object]) -> None:
        """Copy one event sequence for a single SDK request."""

        self._events = list(events)
        self.closed = False

    async def __aenter__(self) -> FakeAsyncStream:
        """Return this stream as the request-owned context manager."""

        return self

    async def __aexit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object | None,
    ) -> None:
        """Close the stream for success, failure, or cancellation."""

        await self.close()

    def __aiter__(self) -> AsyncIterator[object]:
        """Return a fresh async iterator over the queued events."""

        return self._iterate()

    async def _iterate(self) -> AsyncIterator[object]:
        """Yield events or raise a queued exception in order."""

        for event in self._events:
            if isinstance(event, asyncio.Event):
                await event.wait()
                continue
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self) -> None:
        """Record idempotent stream closure."""

        self.closed = True


class RecordingSDKResource:
    """Return queued streams while recording exact official SDK keyword arguments."""

    def __init__(self, outcomes: Sequence[Sequence[object]]) -> None:
        """Copy one event list for every expected create call."""

        self._outcomes = [list(outcome) for outcome in outcomes]
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []
        self.streams: list[FakeAsyncStream] = []

    async def create(self, **kwargs: object) -> FakeAsyncStream:
        """Return the next stream without contacting a Provider."""

        self.calls += 1
        self.kwargs.append(dict(kwargs))
        if not self._outcomes:
            raise AssertionError("RecordingSDKResource outcome queue is empty")
        stream = FakeAsyncStream(self._outcomes.pop(0))
        self.streams.append(stream)
        return stream


class _FakeChatNamespace:
    """Expose the official `client.chat.completions` resource path."""

    def __init__(self, outcomes: Sequence[Sequence[object]]) -> None:
        """Create the Chat Completions recording resource."""

        self.completions = RecordingSDKResource(outcomes)


class FakeOpenAIClient:
    """Expose independent Chat and Responses resources without network access."""

    def __init__(
        self,
        *,
        chat_outcomes: Sequence[Sequence[object]],
        responses_outcomes: Sequence[Sequence[object]],
    ) -> None:
        """Create isolated resource queues and an observable close flag."""

        self.chat = _FakeChatNamespace(chat_outcomes)
        self.responses = RecordingSDKResource(responses_outcomes)
        self.closed = False

    async def close(self) -> None:
        """Record deterministic client cleanup."""

        self.closed = True


class RecordingOpenAIClientBuilder:
    """Return queued fake clients and record exact AsyncOpenAI constructor kwargs."""

    def __init__(self, clients: Sequence[FakeOpenAIClient]) -> None:
        """Copy the expected client sequence for independent Gateway invocations."""

        self._clients = list(clients)
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> AsyncOpenAI:
        """Return the next client through the production constructor type."""

        self.calls += 1
        self.kwargs.append(dict(kwargs))
        if not self._clients:
            raise AssertionError("OpenAI client queue is empty")
        return cast(AsyncOpenAI, self._clients.pop(0))


def chat_chunk(*, content: str | None = None, finish_reason: str | None = None, tool_calls: list[dict[str, object]] | None = None) -> ChatCompletionChunk:
    """Build a concrete SDK Chat chunk, including all required transport fields."""
    from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, Choice, ChoiceDelta
    return ChatCompletionChunk(id="chat-test", choices=[Choice(index=0, delta=ChoiceDelta(content=content, tool_calls=tool_calls), finish_reason=finish_reason)], created=0, model="test-model", object="chat.completion.chunk")


def response_event(kind: str, sequence: int, **fields: object) -> ResponseStreamEvent:
    """Validate a complete fixture through the installed official SDK event union."""
    return _RESPONSE_EVENT_ADAPTER.validate_python({"type": kind, "sequence_number": sequence, **fields})


def response_body(output: list[dict[str, object]], status: str = "completed") -> dict[str, object]:
    """Supply the official response's required fields without a real Provider."""
    return dict(id="resp-test", created_at=0, model="test-model", object="response", output=output, status=status, parallel_tool_calls=False, tool_choice="auto", tools=[])


def responses_events(message: AIMessage) -> list[object]:
    """Translate a graph-level answer to complete typed Responses output events."""
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
    """Queue graph answers; each SDK resource request consumes one answer."""

    def __init__(self, outcomes: Sequence[object] | None = None) -> None:
        """Retain queued outcomes and exact serialized SDK input history."""
        self.outcomes = list(outcomes or [])
        self.calls = 0
        self.message_calls: list[list[dict[str, object]]] = []
        self.streams: list[FakeAsyncStream] = []

    def queue(self, *outcomes: object) -> None:
        """Append answers without replacing prior history."""
        self.outcomes.extend(outcomes)

    async def create_stream(self, responses: bool, **kwargs: object) -> FakeAsyncStream:
        """Convert a queued answer to the selected protocol's concrete SDK events."""
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
    """Expose one concrete SDK resource backed by the graph outcome queue."""

    def __init__(self, sequence: FakeModelSequence, responses: bool) -> None:
        """Bind a protocol without guessing from request content."""
        self.sequence = sequence
        self.responses = responses

    async def create(self, **kwargs: object) -> FakeAsyncStream:
        """Record and consume one graph answer at the SDK boundary."""
        return await self.sequence.create_stream(self.responses, **kwargs)


class RecordingSequenceClientBuilder:
    """Construct a fresh SDK fake per invocation while sharing graph outcomes."""

    def __init__(self, sequence: FakeModelSequence) -> None:
        """Keep diagnostic credentials masked, like the earlier graph fixture."""
        self.sequence = sequence
        self.calls = 0
        self.kwargs: dict[str, object] = {}
        self.clients: list[FakeOpenAIClient] = []

    def __call__(self, **kwargs: object) -> AsyncOpenAI:
        """Record constructor metadata and inject the selected resource queues."""
        from pydantic import SecretStr
        self.calls += 1
        self.kwargs = {**kwargs, "api_key": SecretStr(kwargs["api_key"])}
        client = FakeOpenAIClient(chat_outcomes=[], responses_outcomes=[])
        client.chat.completions = _SequenceResource(self.sequence, False)
        client.responses = _SequenceResource(self.sequence, True)
        self.clients.append(client)
        return cast(AsyncOpenAI, client)


class CancellationAwareModel(FakeModelSequence):
    """Expose cancellation reaching an active SDK iterator to service tests."""

    def __init__(self) -> None:
        """Create observable lifecycle gates for the one blocking invocation."""
        super().__init__()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.release = asyncio.Event()

    async def create_stream(self, responses: bool, **kwargs: object) -> FakeAsyncStream:
        """Return a stream whose iteration, rather than creation, blocks."""
        owner = self
        class BlockingStream(FakeAsyncStream):
            """Block until released or cancelled and record deterministic finalization."""
            async def _iterate(self) -> AsyncIterator[object]:
                """Expose the active iteration lifetime to the owning fixture."""
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
    """Translate a canonical graph answer into complete Chat completion events."""
    import json
    if message.tool_calls:
        return [chat_chunk(tool_calls=[{"index": index, "id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"], separators=(",", ":"))}} for index, call in enumerate(message.tool_calls)]), chat_chunk(finish_reason="tool_calls")]
    return [chat_chunk(content=message.content), chat_chunk(finish_reason="stop")]
