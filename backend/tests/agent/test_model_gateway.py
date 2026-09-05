from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

import httpx2 as httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr, ValidationError

from harness_shell_sidecar.agent.contracts import (
    ApiType,
    ExecuteCommandArguments,
    ModelApiConfig,
)
from harness_shell_sidecar.agent.executor import AgentCancelled
from harness_shell_sidecar.agent.model_gateway import (
    MODEL_REQUEST_TIMEOUT_SECONDS,
    MODEL_RETRY_DELAYS_SECONDS,
    ModelGateway,
    ModelGatewayError,
)
from harness_shell_sidecar.telemetry import ConsoleLogFormatter

from .fakes import (
    CancellationAwareModel,
    FakeModelSequence,
    RecordingSequenceClientBuilder,
    instant_sleep,
)


def _config(api_type: ApiType) -> ModelApiConfig:
    """Build one enabled non-secret model config for gateway tests."""

    now = datetime.now(UTC)
    return ModelApiConfig(
        api_config_id=uuid4(),
        display_name="test-provider",
        api_type=api_type,
        base_url="https://provider.example/v1/",
        model="test-model",
        api_key_credential_id=uuid4(),
        enabled=True,
        created_at=now,
        updated_at=now,
    )


def responses_config() -> ModelApiConfig:
    """Build a config which explicitly selects the Responses API."""

    return _config(ApiType.RESPONSES)


def chat_config() -> ModelApiConfig:
    """Build a config which explicitly selects Chat Completions."""

    return _config(ApiType.CHAT_COMPLETIONS)


async def _invoke(
    gateway: ModelGateway,
    config: ModelApiConfig,
    cancelled: asyncio.Event | None = None,
    sink: RecordingTextSink | None = None,
) -> AIMessage:
    """Invoke the gateway with one stable HumanMessage input."""

    return await gateway.invoke(
        config,
        SecretStr("key"),
        [HumanMessage(content="hi")],
        cancelled or asyncio.Event(),
        sink or RecordingTextSink(),
    )


class RecordingTextSink:
    """Record exact visible deltas without adding transport behavior."""

    def __init__(self) -> None:
        """Create one empty per-invocation delta list."""

        self.deltas: list[str] = []  # Exact visible chunks in Provider order.

    async def text_delta(self, delta: str) -> None:
        """Append one exact visible Provider delta."""

        self.deltas.append(delta)


def test_text_sink_limit_failure_propagates_without_model_error_mapping() -> None:
    """Let the stream owner durably map its own response-size failure."""

    class StreamLimitError(RuntimeError):
        """Represent the later publisher's stable limit failure contract."""

        error_code = "AGENT_RESPONSE_TOO_LARGE"

    class FailingSink(RecordingTextSink):
        """Fail when the first visible delta reaches the stream boundary."""

        async def text_delta(self, delta: str) -> None:
            """Reject the delta without changing its error identity."""

            raise StreamLimitError(delta)

    async def scenario() -> None:
        model = FakeModelSequence([AIMessage(content="too large")])
        failure_sink = FailingSink()

        with pytest.raises(StreamLimitError):
            await _invoke(
                ModelGateway(
                    client_builder=RecordingSequenceClientBuilder(model),
                    sleep=instant_sleep,
                ),
                responses_config(),
                sink=failure_sink,
            )

        assert model.calls == 1

    asyncio.run(scenario())


def test_network_timeout_retries_five_times_then_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Attempt exactly once plus five deterministic timeout retries."""

    async def scenario() -> None:
        failure = openai.APITimeoutError(request=httpx.Request("POST", "https://provider.example/v1/responses"))
        model = FakeModelSequence([failure] * 6)
        builder = RecordingSequenceClientBuilder(model)
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            """Record retry delays without wall-clock waiting."""

            delays.append(delay)

        gateway = ModelGateway(client_builder=builder, sleep=record_sleep)
        caplog.set_level(
            logging.INFO,
            logger="harness_shell_sidecar.agent.model_gateway",
        )

        with pytest.raises(ModelGatewayError) as error:
            await _invoke(gateway, chat_config())

        assert error.value.error_code == "MODEL_NETWORK_TIMEOUT"
        assert model.calls == 6
        assert delays == list(MODEL_RETRY_DELAYS_SECONDS)
        assert builder.calls == 1
        terminal_records = [
            record
            for record in caplog.records
            if getattr(record, "harness_event", None) == "model_network_timeout"
        ]
        assert len(terminal_records) == 1
        assert terminal_records[0].harness_fields["error_code"] == (
            "MODEL_NETWORK_TIMEOUT"
        )
        assert error.value.__cause__ is failure

    asyncio.run(scenario())


def test_timeout_in_cause_chain_retries_then_succeeds() -> None:
    """Classify timeouts only by type while traversing the explicit cause chain."""

    async def scenario() -> None:
        wrapped = RuntimeError("provider wrapper")
        wrapped.__cause__ = asyncio.TimeoutError()
        model = FakeModelSequence([wrapped, AIMessage(content="done")])
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=instant_sleep,
        )

        result = await _invoke(gateway, chat_config())

        assert result.content == "done"
        assert model.calls == 2

    asyncio.run(scenario())


def _status_error(
    error_type: type[openai.APIStatusError],
    status: int,
    *,
    body: object | None = None,
    request_id: str | None = None,
) -> Exception:
    """Build an OpenAI status exception without a live HTTP request."""

    response = httpx.Response(
        status,
        headers={"x-request-id": request_id} if request_id is not None else None,
        request=httpx.Request("POST", "https://provider.example/v1/responses"),
    )
    return error_type("provider rejected request", response=response, body=body)


def test_provider_failure_logs_safe_metadata_without_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep Provider response text out of stderr while retaining stable metadata."""

    async def scenario() -> None:
        failure = _status_error(
            openai.AuthenticationError,
            401,
            body={
                "error": {
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                    "message": "provider-body-marker",
                }
            },
            request_id="req-provider-123",
        )
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(FakeModelSequence([failure])),
            sleep=instant_sleep,
        )
        caplog.set_level(
            logging.INFO,
            logger="harness_shell_sidecar.agent.model_gateway",
        )

        with pytest.raises(ModelGatewayError) as raised:
            await _invoke(gateway, responses_config())

        record = next(
            item
            for item in caplog.records
            if getattr(item, "harness_event", None) == "model_request_failed"
        )
        assert raised.value.error_code == "MODEL_REQUEST_FAILED"
        assert raised.value.__cause__ is failure
        fields = record.harness_fields
        encoded = ConsoleLogFormatter().format(record)
        assert fields["http_status"] == 401
        assert fields["provider_error_code"] == "invalid_api_key"
        assert fields["provider_request_id"] == "req-provider-123"
        assert fields["exception_type"] == "openai.AuthenticationError"
        assert "exception_text" not in fields
        assert "http_response_body" not in fields
        assert "provider-body-marker" not in encoded

    asyncio.run(scenario())


def _schema_error() -> ValidationError:
    """Build a representative local schema validation failure."""

    try:
        ExecuteCommandArguments(command=7)  # type: ignore[arg-type]
    except ValidationError as error:
        return error
    raise AssertionError("strict schema unexpectedly accepted an integer")


@pytest.mark.parametrize(
    "failure",
    [
        _status_error(openai.AuthenticationError, 401),
        _status_error(openai.RateLimitError, 429),
        _status_error(openai.InternalServerError, 500),
        _schema_error(),
        RuntimeError("unknown"),
    ],
    ids=["authentication", "rate-limit", "server-5xx", "schema", "unknown"],
)
def test_non_timeout_failures_do_not_retry(failure: Exception) -> None:
    """Fail once for every non-timeout category, including provider status errors."""

    async def scenario() -> None:
        model = FakeModelSequence([failure])
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=instant_sleep,
        )

        with pytest.raises(ModelGatewayError) as error:
            await _invoke(gateway, responses_config())

        assert error.value.error_code == "MODEL_REQUEST_FAILED"
        assert model.calls == 1

    asyncio.run(scenario())


def test_unrecognized_metadata_does_not_retry() -> None:
    """Ignore non-output metadata without retrying the provider."""

    async def scenario() -> None:
        model = FakeModelSequence([HumanMessage(content="wrong type")])
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=instant_sleep,
        )

        result = await _invoke(gateway, chat_config())
        assert result.content == ""
        assert model.calls == 1

    asyncio.run(scenario())


def test_cancellation_stops_an_active_model_request() -> None:
    """Cancel and await the in-flight model task without starting another attempt."""

    async def scenario() -> None:
        blocker = asyncio.Event()
        model = FakeModelSequence([blocker])
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=instant_sleep,
        )
        cancelled = asyncio.Event()

        request = asyncio.create_task(_invoke(gateway, chat_config(), cancelled))
        while model.calls == 0:
            await asyncio.sleep(0)
        cancelled.set()
        with pytest.raises(AgentCancelled):
            await request

        assert model.calls == 1

    asyncio.run(scenario())


def test_cancellation_stops_retry_backoff_before_next_attempt() -> None:
    """Interrupt a timeout backoff without dispatching the next model request."""

    async def scenario() -> None:
        sleeping = asyncio.Event()

        async def blocking_sleep(_delay: float) -> None:
            """Block the first retry until cancellation wins the race."""

            await sleeping.wait()

        model = FakeModelSequence(
            [openai.APITimeoutError(request=httpx.Request("POST", "https://provider.example/v1/responses")), AIMessage(content="must not run")]
        )
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=blocking_sleep,
        )
        cancelled = asyncio.Event()

        request = asyncio.create_task(_invoke(gateway, chat_config(), cancelled))
        while model.calls == 0:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        cancelled.set()
        with pytest.raises(AgentCancelled):
            await request

        assert model.calls == 1

    asyncio.run(scenario())


def test_outer_task_cancellation_stops_active_model_operation() -> None:
    """Propagate caller cancellation into the provider task before returning."""

    async def scenario() -> None:
        model = CancellationAwareModel()
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=instant_sleep,
        )
        request = asyncio.create_task(_invoke(gateway, chat_config()))
        await model.started.wait()

        request.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await request
            assert model.stopped.is_set()
        finally:
            model.release.set()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_outer_task_cancellation_stops_retry_sleep() -> None:
    """Propagate caller cancellation into a timeout backoff task before returning."""

    async def scenario() -> None:
        sleeping = asyncio.Event()
        stopped = asyncio.Event()
        release = asyncio.Event()

        async def blocking_sleep(_delay: float) -> None:
            """Expose the retry sleeper lifecycle to the cancellation assertion."""

            sleeping.set()
            try:
                await release.wait()
            finally:
                stopped.set()

        model = FakeModelSequence([openai.APITimeoutError(request=httpx.Request("POST", "https://provider.example/v1/responses"))])
        gateway = ModelGateway(
            client_builder=RecordingSequenceClientBuilder(model),
            sleep=blocking_sleep,
        )
        request = asyncio.create_task(_invoke(gateway, chat_config()))
        await sleeping.wait()

        request.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await request
            assert stopped.is_set()
        finally:
            release.set()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_chat_messages_preserve_roles_tool_id_and_arguments() -> None:
    from langchain_core.messages import SystemMessage, ToolMessage
    from harness_shell_sidecar.agent.model_gateway import _serialize_chat_messages
    from .fakes import make_tool_call
    messages = [SystemMessage(content="rules", additional_kwargs={"__openai_role__": "developer"}), HumanMessage(content="inspect"), AIMessage(content="", tool_calls=[make_tool_call("call-7", "pwd")]), ToolMessage(content='{"ok":true}', tool_call_id="call-7")]
    assert _serialize_chat_messages(messages) == [
        {"role": "developer", "content": "rules"},
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-7", "type": "function", "function": {"name": "execute_command", "arguments": '{"command":"pwd"}'}}]},
        {"role": "tool", "content": '{"ok":true}', "tool_call_id": "call-7"},
    ]


def test_responses_replay_json_roundtrip_and_cross_config() -> None:
    from harness_shell_sidecar.agent.model_gateway import _serialize_responses_input
    from .fakes import make_tool_call
    config = responses_config()
    item = {"type": "function_call", "id": "fc-7", "call_id": "call-7", "name": "execute_command", "arguments": '{"command":"pwd"}', "status": "completed"}
    message = AIMessage(content="", tool_calls=[make_tool_call("call-7", "pwd")], additional_kwargs={"harness_responses_replay": {"schema_version": 1, "api_config_id": str(config.api_config_id), "items": [item]}})
    assert _serialize_responses_input(config, [message]) == [item]
    assert _serialize_responses_input(responses_config(), [message]) == [{"type": "function_call", "call_id": "call-7", "name": "execute_command", "arguments": '{"command":"pwd"}'}]
    message.additional_kwargs["harness_responses_replay"]["unknown"] = True
    with pytest.raises(ModelGatewayError, match="MODEL_RESPONSE_INVALID"):
        _serialize_responses_input(responses_config(), [message])


@pytest.mark.parametrize("kind", ["function", "content", "args", "tool_id"])
def test_unsupported_history_is_rejected(kind: str) -> None:
    from langchain_core.messages import FunctionMessage, ToolMessage
    from harness_shell_sidecar.agent.model_gateway import _serialize_chat_messages, _serialize_responses_input
    from .fakes import make_tool_call
    message = AIMessage(content="", tool_calls=[make_tool_call("call-7", "pwd")])
    if kind == "function":
        message = FunctionMessage(content="x", name="old")
    elif kind == "content":
        message = HumanMessage(content=[{"type": "text", "text": "x"}])
    elif kind == "args":
        message.tool_calls[0]["args"] = []
    else:
        message = ToolMessage(content="x", tool_call_id="")
    with pytest.raises(ModelGatewayError, match="MODEL_RESPONSE_INVALID"):
        _serialize_chat_messages([message])
    with pytest.raises(ModelGatewayError, match="MODEL_RESPONSE_INVALID"):
        _serialize_responses_input(responses_config(), [message])


def test_fake_openai_client_records_resource_and_close_lifecycle() -> None:
    """Expose the exact client ownership contract used by gateway tests."""

    async def scenario() -> None:
        from .fakes import FakeOpenAIClient
        client = FakeOpenAIClient(chat_outcomes=[[]], responses_outcomes=[])
        stream = await client.chat.completions.create(
            model="test-model",
            messages=[],
            tools=[],
            parallel_tool_calls=False,
            stream=True,
        )
        async with stream:
            assert [event async for event in stream] == []
        await client.close()

        assert client.chat.completions.calls == 1
        assert stream.closed is True
        assert client.closed is True

    asyncio.run(scenario())


@pytest.mark.parametrize("tool", [False, True])
def test_chat_stream_emits_and_aggregates_exact_text_or_tool(tool: bool) -> None:
    from harness_shell_sidecar.agent.model_gateway import _parse_chat_completions_stream, _InvocationState
    from .fakes import FakeAsyncStream, chat_chunk, make_tool_call
    async def scenario() -> None:
        sink = RecordingTextSink()
        if tool:
            events = [chat_chunk(tool_calls=[{"index": 0, "id": "call-1", "type": "function", "function": {"name": "execute_command", "arguments": '{"command":"pw'}}]), chat_chunk(tool_calls=[{"index": 0, "function": {"arguments": 'd"}'}}]), chat_chunk(finish_reason="tool_calls")]
        else:
            events = [chat_chunk(content=" hello"), chat_chunk(content="\nworld "), chat_chunk(finish_reason="stop")]
        result = await _parse_chat_completions_stream(FakeAsyncStream(events), sink, _InvocationState())
        assert type(result) is AIMessage
        assert result.tool_calls == ([make_tool_call("call-1", "pwd")] if tool else [])
        assert sink.deltas == ([] if tool else [" hello\nworld "])
        assert result.content == ("" if tool else " hello\nworld ")
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["choice", "multiple", "negative", "conflict", "bad_json", "list", "mixed", "reverse", "duplicate", "eof", "length", "content_filter", "function_call"])
def test_chat_stream_provider_variations(kind: str) -> None:
    from harness_shell_sidecar.agent.model_gateway import _parse_chat_completions_stream, _InvocationState
    from .fakes import FakeAsyncStream, chat_chunk
    async def scenario() -> None:
        call = {"index": 0, "id": "call-1", "type": "function", "function": {"name": "execute_command", "arguments": '{"command":"pwd"}'}}
        events = [chat_chunk(content="x"), chat_chunk(finish_reason="stop")]
        if kind == "choice": events[0].choices[0].index = 1
        elif kind == "multiple": events[0].choices.append(events[0].choices[0])
        elif kind == "negative":
            call["index"] = -1
            events = [chat_chunk(tool_calls=[call]), chat_chunk(finish_reason="tool_calls")]
        elif kind in ("bad_json", "list"):
            call["function"]["arguments"] = "{" if kind == "bad_json" else "[]"
            events = [chat_chunk(tool_calls=[call]), chat_chunk(finish_reason="tool_calls")]
        elif kind == "conflict":
            events = [chat_chunk(tool_calls=[call]), chat_chunk(tool_calls=[{"index": 0, "id": "different"}]), chat_chunk(finish_reason="tool_calls")]
        elif kind == "mixed": events.insert(1, chat_chunk(tool_calls=[call]))
        elif kind == "reverse": events.insert(0, chat_chunk(tool_calls=[call]))
        elif kind == "duplicate": events.append(chat_chunk(finish_reason="stop"))
        elif kind == "eof": events.pop()
        else: events[-1] = chat_chunk(finish_reason=kind)
        sink = RecordingTextSink()
        if kind in ("bad_json", "list"):
            with pytest.raises(ModelGatewayError, match="MODEL_RESPONSE_INVALID"):
                await _parse_chat_completions_stream(FakeAsyncStream(events), sink, _InvocationState())
        else:
            result = await _parse_chat_completions_stream(FakeAsyncStream(events), sink, _InvocationState())
            assert result.content == ("" if kind in ("negative", "conflict") else "x")
            assert bool(result.tool_calls) == (kind in ("negative", "conflict", "mixed", "reverse"))
            assert "".join(sink.deltas) == ("" if result.tool_calls else result.content)
    asyncio.run(scenario())


@pytest.mark.parametrize("tool", [False, True])
def test_responses_stream_captures_ordered_replay(tool: bool) -> None:
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _InvocationState, _serialize_responses_input
    from .fakes import FakeAsyncStream, responses_events, response_event, make_tool_call
    async def scenario() -> None:
        config = responses_config()
        sink = RecordingTextSink()
        message = AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")]) if tool else AIMessage(content=" hello\nworld ")
        events = responses_events(message)
        if tool:
            reasoning = dict(type="reasoning", id="rs-0", status="completed", summary=[], encrypted_content="opaque-marker")
            for event in events:
                event.sequence_number += 1
                if hasattr(event, "output_index"): event.output_index += 1
            events[-1].response.output.insert(0, __import__("openai").types.responses.ResponseReasoningItem(**reasoning))
            events.insert(0, response_event("response.output_item.done", 0, output_index=0, item=reasoning))
        result = await _parse_responses_stream(FakeAsyncStream(events), config, sink, _InvocationState())
        assert result.content == message.content
        assert result.tool_calls == message.tool_calls
        replay = result.additional_kwargs["harness_responses_replay"]
        assert replay["api_config_id"] == str(config.api_config_id)
        assert [item["type"] for item in replay["items"]] == (["reasoning", "function_call"] if tool else ["message"])
        assert _serialize_responses_input(config, [result]) == replay["items"]
        assert sink.deltas == ([] if tool else [" hello\nworld "])
    asyncio.run(scenario())


@pytest.mark.parametrize("omit", ["reasoning_status", "argument_name", "metadata", "all"])
def test_responses_accepts_sparse_provider_events(omit: str) -> None:
    """Optional wire metadata must not prevent a complete tool call and replay."""
    from openai._models import construct_type
    from openai.types.responses import ResponseStreamEvent
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _InvocationState, _serialize_responses_input
    from .fakes import FakeAsyncStream, response_body

    async def scenario() -> None:
        reasoning = dict(type="reasoning", id="rs-1", summary=[], encrypted_content="opaque", status="completed")
        call = dict(type="function_call", id="fc-1", call_id="call-1", name="execute_command", arguments='{"command":"pwd"}', status="completed")
        done = dict(type="response.function_call_arguments.done", sequence_number=3, output_index=1, item_id="fc-1", arguments='{"command":"pwd"}', name="execute_command")
        if omit in ("reasoning_status", "all"):
            reasoning.pop("status")
        if omit in ("argument_name", "all"):
            done.pop("name")
        terminal = response_body([reasoning, call])
        if omit in ("metadata", "all"):
            terminal.pop("created_at")
            terminal["usage"] = {"provider_specific_tokens": 5}
        wire = [
            dict(type="response.output_item.added", sequence_number=0, output_index=0, item=reasoning),
            dict(type="response.output_item.done", sequence_number=1, output_index=0, item=reasoning),
            dict(type="response.function_call_arguments.delta", sequence_number=2, output_index=1, item_id="fc-1", delta='{"command":"pwd"}'),
            done,
            dict(type="response.output_item.done", sequence_number=4, output_index=1, item=call),
            dict(type="response.completed", sequence_number=5, response=terminal),
        ]
        # Match the SDK's permissive construction used on real HTTP streams.
        events = [construct_type(type_=ResponseStreamEvent, value=event) for event in wire]
        config = responses_config()
        sink = RecordingTextSink()
        result = await _parse_responses_stream(FakeAsyncStream(events), config, sink, _InvocationState())
        assert result.tool_calls == [{"name": "execute_command", "args": {"command": "pwd"}, "id": "call-1", "type": "tool_call"}]
        replay = _serialize_responses_input(config, [result])
        assert replay[0] == {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "opaque", "status": "completed"}
        assert replay[1] == call
        assert sink.deltas == []
    asyncio.run(scenario())


@pytest.mark.parametrize("numbering", ["missing", "repeated", "reset", "string"])
def test_responses_uses_arrival_order_without_sequence_metadata(numbering: str) -> None:
    """Provider sequence metadata cannot reject an otherwise complete SSE stream."""
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _InvocationState
    from .fakes import FakeAsyncStream, responses_events

    async def scenario() -> None:
        events = responses_events(AIMessage(content="answer"))
        for index, event in enumerate(events):
            if numbering == "missing":
                del event.sequence_number
            elif numbering == "repeated":
                event.sequence_number = 0
            elif numbering == "reset":
                event.sequence_number = 10 - index
            else:
                event.sequence_number = str(index)
        sink = RecordingTextSink()
        result = await _parse_responses_stream(FakeAsyncStream(events), responses_config(), sink, _InvocationState())
        assert result.content == "answer"
        assert sink.deltas == ["answer"]
        assert result.tool_calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["unknown", "index", "text_done", "item", "duplicate", "eof", "output", "arguments", "mixed", "failed", "incomplete", "error"])
def test_responses_stream_provider_variations(kind: str) -> None:
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _InvocationState
    from .fakes import FakeAsyncStream, responses_events, response_event, make_tool_call
    async def scenario() -> None:
        events = responses_events(AIMessage(content="x"))
        if kind == "unknown": events[0] = events[0].model_copy(update={"type": "unknown"})
        elif kind == "index": events[1].output_index = 1
        elif kind == "text_done": events[1].text = "wrong"
        elif kind == "item": events[2].item.id = "wrong"
        elif kind == "duplicate": events.append(events[-1].model_copy(update={"sequence_number": 9}))
        elif kind == "eof": events.pop()
        elif kind == "output": events[-1].response.output = []
        elif kind == "arguments":
            events = responses_events(AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")]))
            events[1].arguments = "{}"
        elif kind == "mixed": events.insert(1, response_event("response.function_call_arguments.delta", 1, item_id="fc-0", output_index=1, delta="{}"))
        elif kind == "error": events = [response_event("error", 0, code="failure", message="provider-secret-marker", param=None)]
        else:
            events[-1] = response_event("response." + kind, 3, response=events[-1].response.model_copy(update={"status": kind}))
        sink = RecordingTextSink()
        if kind in ("failed", "error"):
            with pytest.raises(ModelGatewayError) as raised:
                await _parse_responses_stream(FakeAsyncStream(events), responses_config(), sink, _InvocationState())
            assert raised.value.error_code == "MODEL_REQUEST_FAILED"
            assert "provider-secret-marker" not in str(raised.value)
        else:
            result = await _parse_responses_stream(FakeAsyncStream(events), responses_config(), sink, _InvocationState())
            assert result.content == ("" if kind == "arguments" else "x")
            assert "".join(sink.deltas) == result.content
    asyncio.run(scenario())


@pytest.mark.parametrize("api_type", list(ApiType))
def test_official_client_dispatch_and_ownership(api_type: ApiType) -> None:
    from .fakes import FakeOpenAIClient, RecordingOpenAIClientBuilder, responses_events, chat_chunk
    async def scenario() -> None:
        client = FakeOpenAIClient(chat_outcomes=[[chat_chunk(content="done"), chat_chunk(finish_reason="stop")]], responses_outcomes=[responses_events(AIMessage(content="done"))])
        builder = RecordingOpenAIClientBuilder([client])
        result = await _invoke(ModelGateway(client_builder=builder), _config(api_type))
        assert result.content == "done"
        assert builder.kwargs == [{"api_key": "key", "base_url": "https://provider.example/v1/", "timeout": 60, "max_retries": 0}]
        resource = client.responses if api_type is ApiType.RESPONSES else client.chat.completions
        other = client.chat.completions if api_type is ApiType.RESPONSES else client.responses
        assert resource.calls == 1 and other.calls == 0
        request = resource.kwargs[0]
        assert request["model"] == "test-model"
        assert request["parallel_tool_calls"] is False and request["stream"] is True
        assert request.get("input", request.get("messages")) == [{"role": "user", "content": "hi"}]
        tool = request["tools"][0]
        assert tool["type"] == "function"
        definition = tool if api_type is ApiType.RESPONSES else tool["function"]
        assert definition["name"] == "execute_command" and definition["strict"] is True
        assert definition["parameters"]["additionalProperties"] is False
        assert set(request) == ({"model", "input", "tools", "parallel_tool_calls", "include", "stream"} if api_type is ApiType.RESPONSES else {"model", "messages", "tools", "parallel_tool_calls", "stream"})
        if api_type is ApiType.RESPONSES: assert request["include"] == ["reasoning.encrypted_content"]
        assert resource.streams[0].closed and client.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("api_type", list(ApiType))
@pytest.mark.parametrize("visible", [False, True])
def test_official_timeout_retry_boundary(api_type: ApiType, visible: bool) -> None:
    import httpx2
    from .fakes import FakeOpenAIClient, RecordingOpenAIClientBuilder, responses_events, response_event, chat_chunk
    async def scenario() -> None:
        failure = openai.APITimeoutError(request=httpx2.Request("POST", "https://provider.example/v1/responses"))
        if api_type is ApiType.RESPONSES:
            first = response_event("response.output_text.delta", 0, item_id="msg-0", output_index=0, content_index=0, delta="partial", logprobs=[]) if visible else response_event("response.function_call_arguments.delta", 0, item_id="fc-0", output_index=0, delta="{")
            success = responses_events(AIMessage(content="done"))
        else:
            first = chat_chunk(content="partial") if visible else chat_chunk(tool_calls=[{"index": 0, "id": "call-1", "type": "function", "function": {"name": "execute_command", "arguments": "{"}}])
            success = [chat_chunk(content="done"), chat_chunk(finish_reason="stop")]
        outcomes = [[first, failure], success]
        client = FakeOpenAIClient(chat_outcomes=outcomes, responses_outcomes=outcomes)
        sink = RecordingTextSink()
        gateway = ModelGateway(client_builder=RecordingOpenAIClientBuilder([client]), sleep=instant_sleep)
        # No buffered draft has been published, so either attempt can retry.
        assert (await _invoke(gateway, _config(api_type), sink=sink)).content == "done"
        assert sink.deltas == ["done"]
        resource = client.responses if api_type is ApiType.RESPONSES else client.chat.completions
        assert resource.calls == 2
        assert client.closed and all(stream.closed for stream in resource.streams)
    asyncio.run(scenario())


def test_official_preset_cancel_does_not_construct_client() -> None:
    from .fakes import RecordingOpenAIClientBuilder
    async def scenario() -> None:
        builder = RecordingOpenAIClientBuilder([])
        cancel = asyncio.Event()
        cancel.set()
        with pytest.raises(AgentCancelled): await _invoke(ModelGateway(client_builder=builder), chat_config(), cancel)
        assert builder.calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("earlier", [False, True])
def test_official_cleanup_preserves_first_failure(earlier: bool) -> None:
    from .fakes import FakeOpenAIClient, FakeAsyncStream, RecordingOpenAIClientBuilder, chat_chunk
    async def scenario() -> None:
        failure = RuntimeError("provider-marker")
        close_failure = RuntimeError("close-marker")
        client = FakeOpenAIClient(chat_outcomes=[], responses_outcomes=[])
        stream = FakeAsyncStream([failure] if earlier else [chat_chunk(finish_reason="stop")])
        async def broken_close() -> None:
            """Inject a stream cleanup failure after marking closure."""
            stream.closed = True
            raise close_failure
        async def create(**kwargs: object):
            """Return the owned stream for cleanup fault injection."""
            return stream
        stream.close = broken_close
        client.chat.completions.create = create
        with pytest.raises(ModelGatewayError) as raised:
            await _invoke(ModelGateway(client_builder=RecordingOpenAIClientBuilder([client])), chat_config())
        assert raised.value.__cause__ is (failure if earlier else close_failure)
        assert client.closed and stream.closed
    asyncio.run(scenario())


def test_official_standalone_client_close_failure_is_visible() -> None:
    from .fakes import FakeOpenAIClient, RecordingOpenAIClientBuilder, chat_chunk
    async def scenario() -> None:
        client = FakeOpenAIClient(chat_outcomes=[[chat_chunk(finish_reason="stop")]], responses_outcomes=[])
        failure = RuntimeError("client-close")
        async def close() -> None:
            """Raise a standalone cleanup error after a successful request."""
            raise failure
        client.close = close
        with pytest.raises(RuntimeError) as raised:
            await _invoke(ModelGateway(client_builder=RecordingOpenAIClientBuilder([client])), chat_config())
        assert raised.value is failure
    asyncio.run(scenario())


def test_responses_lifecycle_split_text_and_arguments() -> None:
    from .fakes import FakeAsyncStream, response_event, response_body
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _InvocationState
    async def scenario() -> None:
        output = dict(type="message", id="msg-0", role="assistant", status="completed", content=[dict(type="output_text", text=" hello\nworld ", annotations=[], logprobs=[])])
        events = []
        def add(kind: str, **fields: object) -> None:
            """Assign strictly increasing sequence numbers to literal lifecycle fixtures."""
            events.append(response_event(kind, len(events), **fields))
        add("response.created", response=response_body([], "in_progress"))
        add("response.in_progress", response=response_body([], "in_progress"))
        add("response.output_item.added", output_index=0, item={**output, "status": "in_progress", "content": []})
        add("response.content_part.added", output_index=0, item_id="msg-0", content_index=0, part=dict(type="output_text", text="", annotations=[], logprobs=[]))
        for text in [" hello", "\nworld "]:
            add("response.output_text.delta", output_index=0, item_id="msg-0", content_index=0, delta=text, logprobs=[])
        add("response.output_text.done", output_index=0, item_id="msg-0", content_index=0, text=" hello\nworld ", logprobs=[])
        add("response.content_part.done", output_index=0, item_id="msg-0", content_index=0, part=output["content"][0])
        add("response.output_item.done", output_index=0, item=output)
        add("response.completed", response=response_body([output]))
        sink = RecordingTextSink()
        result = await _parse_responses_stream(FakeAsyncStream(events), responses_config(), sink, _InvocationState())
        assert result.content == " hello\nworld " and sink.deltas == [" hello\nworld "]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["raw_event", "missing_delta", "negative_content", "incomplete_item", "duplicate_call", "orphan_delta"])
def test_responses_uses_complete_output_despite_sparse_deltas(kind: str) -> None:
    from .fakes import FakeAsyncStream, responses_events, response_event, make_tool_call
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _InvocationState
    async def scenario() -> None:
        events = responses_events(AIMessage(content="x"))
        if kind == "raw_event": events[0] = {"type": "unknown"}
        elif kind == "missing_delta": del events[0].delta
        elif kind == "negative_content": events[0].content_index = -1
        elif kind == "incomplete_item": events[2].item.status = "in_progress"
        elif kind == "duplicate_call": events = responses_events(AIMessage(content="", tool_calls=[make_tool_call("same", "pwd"), make_tool_call("same", "ls")]))
        elif kind == "orphan_delta": events[2].output_index = 1
        result = await _parse_responses_stream(FakeAsyncStream(events), responses_config(), RecordingTextSink(), _InvocationState())
        assert result.content == ("" if kind == "duplicate_call" else "x")
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("schema_version", True), ("call_id", ""), ("name", ""), ("arguments", "[]"), ("arguments", "{")])
def test_replay_rejects_semantically_invalid_fields_across_configs(field: str, value: object) -> None:
    from harness_shell_sidecar.agent.model_gateway import _serialize_responses_input
    from .fakes import make_tool_call
    config = responses_config()
    item = dict(type="function_call", id="fc-1", call_id="call-1", name="execute_command", arguments='{"command":"pwd"}', status="completed")
    envelope = dict(schema_version=1, api_config_id=str(config.api_config_id), items=[item])
    if field == "schema_version": envelope[field] = value
    else: item[field] = value
    message = AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")], additional_kwargs={"harness_responses_replay": envelope})
    with pytest.raises(ModelGatewayError, match="MODEL_RESPONSE_INVALID"):
        _serialize_responses_input(responses_config(), [message])


def test_sink_failure_identity_is_preserved_for_any_exception() -> None:
    from .fakes import FakeOpenAIClient, RecordingOpenAIClientBuilder, chat_chunk
    async def scenario() -> None:
        failure = RuntimeError("sink-owned-failure")
        class FailingSink(RecordingTextSink):
            """Represent a publisher failure unrelated to Provider transport."""
            async def text_delta(self, delta: str) -> None:
                """Expose the publisher's original failure object."""
                raise failure
        client = FakeOpenAIClient(chat_outcomes=[[chat_chunk(content="x")]], responses_outcomes=[])
        with pytest.raises(RuntimeError) as raised:
            await _invoke(ModelGateway(client_builder=RecordingOpenAIClientBuilder([client])), chat_config(), sink=FailingSink())
        assert raised.value is failure
        assert client.closed and client.chat.completions.streams[0].closed
    asyncio.run(scenario())


@pytest.mark.parametrize("responses", [False, True])
def test_malformed_sdk_fields_do_not_warn_with_provider_content(responses: bool) -> None:
    import warnings
    from .fakes import FakeAsyncStream, responses_events, chat_chunk
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _parse_chat_completions_stream, _InvocationState
    async def scenario() -> None:
        if responses:
            event = responses_events(AIMessage(content="x"))[0]
            event.delta = {"secret": "provider-body-marker"}
        else:
            event = chat_chunk(content="x")
            event.choices[0].delta.content = {"secret": "provider-body-marker"}
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            sink = RecordingTextSink()
            if responses:
                result = await _parse_responses_stream(FakeAsyncStream([event]), responses_config(), sink, _InvocationState())
            else:
                result = await _parse_chat_completions_stream(FakeAsyncStream([event]), sink, _InvocationState())
            assert result.content == "" and sink.deltas == []
        assert not captured
    asyncio.run(scenario())


def test_responses_split_tool_arguments_and_message_phase_replay() -> None:
    from .fakes import FakeAsyncStream, responses_events, response_event, make_tool_call
    from harness_shell_sidecar.agent.model_gateway import _parse_responses_stream, _InvocationState
    async def scenario() -> None:
        events = responses_events(AIMessage(content="", tool_calls=[make_tool_call("call-1", "pwd")]))
        events[0].delta = '{"command":"pw'
        events.insert(1, response_event("response.function_call_arguments.delta", 1, item_id="fc-0", output_index=0, delta='d"}'))
        for sequence, event in enumerate(events): event.sequence_number = sequence
        sink = RecordingTextSink()
        result = await _parse_responses_stream(FakeAsyncStream(events), responses_config(), sink, _InvocationState())
        assert result.tool_calls == [make_tool_call("call-1", "pwd")]
        assert sink.deltas == []
        events = responses_events(AIMessage(content="answer"))
        events[-2].item.phase = "final_answer"
        events[-1].response.output[0].phase = "final_answer"
        result = await _parse_responses_stream(FakeAsyncStream(events), responses_config(), sink, _InvocationState())
        assert result.additional_kwargs["harness_responses_replay"]["items"][0]["phase"] == "final_answer"
    asyncio.run(scenario())


@pytest.mark.parametrize("api_type", list(ApiType))
@pytest.mark.parametrize("outer", [False, True])
def test_official_cancellation_closes_stream_and_client(api_type: ApiType, outer: bool) -> None:
    from .fakes import FakeOpenAIClient, RecordingOpenAIClientBuilder
    async def scenario() -> None:
        blocker = asyncio.Event()
        client = FakeOpenAIClient(chat_outcomes=[[blocker]], responses_outcomes=[[blocker]])
        resource = client.responses if api_type is ApiType.RESPONSES else client.chat.completions
        cancelled = asyncio.Event()
        task = asyncio.create_task(_invoke(ModelGateway(client_builder=RecordingOpenAIClientBuilder([client])), _config(api_type), cancelled))
        while not resource.streams: await asyncio.sleep(0)
        if outer: task.cancel()
        else: cancelled.set()
        with pytest.raises(asyncio.CancelledError if outer else AgentCancelled): await task
        assert client.closed and resource.streams[0].closed
        assert resource.calls == 1
    asyncio.run(scenario())
