from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
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
from harness_shell_sidecar.telemetry import JsonLogFormatter

from .fakes import (
    CancellationAwareModel,
    FakeBoundModel,
    RecordingModelBuilder,
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
        api_key_secret_ref=uuid4(),
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
) -> AIMessage:
    """Invoke the gateway with one stable HumanMessage input."""

    return await gateway.invoke(
        config,
        SecretStr("key"),
        [HumanMessage(content="hi")],
        cancelled or asyncio.Event(),
    )


def test_responses_config_selects_responses_without_previous_response_id() -> None:
    """Pass the exact Responses flags and disable SDK retries."""

    async def scenario() -> None:
        model = FakeBoundModel([AIMessage(content="done")])
        builder = RecordingModelBuilder(model)
        gateway = ModelGateway(model_builder=builder, sleep=instant_sleep)

        await _invoke(gateway, responses_config())

        assert builder.kwargs["use_responses_api"] is True
        assert builder.kwargs["use_previous_response_id"] is False
        assert builder.kwargs["max_retries"] == 0
        assert builder.kwargs["timeout"] == MODEL_REQUEST_TIMEOUT_SECONDS
        assert builder.kwargs["model"] == "test-model"
        assert builder.kwargs["base_url"] == "https://provider.example/v1/"

    asyncio.run(scenario())


def test_chat_config_binds_the_same_single_strict_command_tool() -> None:
    """Select Chat Completions while preserving the same strict tool schema."""

    async def scenario() -> None:
        model = FakeBoundModel([AIMessage(content="done")])
        builder = RecordingModelBuilder(model)
        gateway = ModelGateway(model_builder=builder, sleep=instant_sleep)

        await _invoke(gateway, chat_config())

        assert builder.kwargs["use_responses_api"] is False
        assert model.bind_kwargs == {"strict": True, "parallel_tool_calls": False}
        assert len(model.bound_tools) == 1
        tool = model.bound_tools[0]
        assert getattr(tool, "name") == "execute_command"
        assert getattr(tool, "args_schema") is ExecuteCommandArguments

    asyncio.run(scenario())


def test_network_timeout_retries_five_times_then_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Attempt exactly once plus five deterministic timeout retries."""

    async def scenario() -> None:
        failure = httpx.ReadTimeout("timeout")
        model = FakeBoundModel([failure] * 6)
        builder = RecordingModelBuilder(model)
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            """Record retry delays without wall-clock waiting."""

            delays.append(delay)

        gateway = ModelGateway(model_builder=builder, sleep=record_sleep)
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
        model = FakeBoundModel([wrapped, AIMessage(content="done")])
        gateway = ModelGateway(
            model_builder=RecordingModelBuilder(model),
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


def test_provider_failure_logs_safe_diagnostics_before_mapping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve safe Provider metadata and the internal cause before code mapping."""

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
            model_builder=RecordingModelBuilder(FakeBoundModel([failure])),
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
        assert record.harness_fields["http_status"] == 401
        assert record.harness_fields["provider_error_code"] == "invalid_api_key"
        assert record.harness_fields["provider_request_id"] == "req-provider-123"
        assert "provider-body-marker" not in JsonLogFormatter().format(record)

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
        model = FakeBoundModel([failure])
        gateway = ModelGateway(
            model_builder=RecordingModelBuilder(model),
            sleep=instant_sleep,
        )

        with pytest.raises(ModelGatewayError) as error:
            await _invoke(gateway, responses_config())

        assert error.value.error_code == "MODEL_REQUEST_FAILED"
        assert model.calls == 1

    asyncio.run(scenario())


def test_non_ai_message_result_does_not_retry() -> None:
    """Reject a model contract violation after a single provider result."""

    async def scenario() -> None:
        model = FakeBoundModel([HumanMessage(content="wrong type")])
        gateway = ModelGateway(
            model_builder=RecordingModelBuilder(model),
            sleep=instant_sleep,
        )

        with pytest.raises(ModelGatewayError) as error:
            await _invoke(gateway, chat_config())

        assert error.value.error_code == "MODEL_RESPONSE_INVALID"
        assert model.calls == 1

    asyncio.run(scenario())


def test_cancellation_stops_an_active_model_request() -> None:
    """Cancel and await the in-flight model task without starting another attempt."""

    async def scenario() -> None:
        blocker = asyncio.Event()
        model = FakeBoundModel([blocker])
        gateway = ModelGateway(
            model_builder=RecordingModelBuilder(model),
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

        model = FakeBoundModel(
            [httpx.ReadTimeout("timeout"), AIMessage(content="must not run")]
        )
        gateway = ModelGateway(
            model_builder=RecordingModelBuilder(model),
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
            model_builder=RecordingModelBuilder(model),
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

        model = FakeBoundModel([httpx.ReadTimeout("timeout")])
        gateway = ModelGateway(
            model_builder=RecordingModelBuilder(model),
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
