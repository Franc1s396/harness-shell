from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from harness_shell_sidecar.agent.api_configs import ApiConfigRepositoryError
from harness_shell_sidecar.agent.contracts import (
    AgentRunStatus,
    AgentTurnInput,
    AgentTurnResult,
)
from harness_shell_sidecar.agent.handlers import register_agent_handlers
from harness_shell_sidecar.agent.service import AgentServiceError
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher

from .conftest import AgentStorage, valid_api_config_input


MAX_JSON_RESPONSE_BYTES = 1_048_576


@dataclass(slots=True)
class FakeAgentService:
    """Record one handler invocation and return, cancel, or raise deterministically."""

    outcome: AgentTurnResult | Exception
    wait_for_cancel: bool = False
    requests: list[AgentTurnInput] = field(default_factory=list)
    api_keys: list[str] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run_turn(
        self,
        request: AgentTurnInput,
        api_key: SecretStr,
        cancelled: asyncio.Event,
        *,
        expected_config: object,
    ) -> AgentTurnResult:
        """Capture decoded values without exposing them through handler errors."""

        self.requests.append(request)
        self.api_keys.append(api_key.get_secret_value())
        self.started.set()
        if self.wait_for_cancel:
            await cancelled.wait()
            return AgentTurnResult(
                conversation_id=request.conversation_id or uuid4(),
                agent_run_id=uuid4(),
                status=AgentRunStatus.CANCELLED,
                final_text=None,
                react_iteration=0,
                error_code="AGENT_CANCELLED",
            )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def application_frame(
    method: str,
    *,
    params: dict[str, object],
    request_id: UUID | None = None,
) -> tuple[UUID, str, dict[str, object]]:
    """Build dispatcher arguments without constructing a transport envelope."""

    return request_id or uuid4(), method, params


def _result() -> AgentTurnResult:
    """Build a successful fake turn result."""

    return AgentTurnResult(
        conversation_id=uuid4(),
        agent_run_id=uuid4(),
        status=AgentRunStatus.COMPLETED,
        final_text="done",
        react_iteration=1,
        error_code=None,
    )


def _turn_params(
    api_config_id: UUID,
    credential_id: UUID,
    *,
    api_key_b64: str | None = None,
) -> dict[str, object]:
    """Build canonical secret turn params for one persisted config."""

    return {
        "conversation_id": None,
        "ssh_session_id": str(uuid4()),
        "api_config_id": str(api_config_id),
        "api_key_credential_id": str(credential_id),
        "api_key_b64": api_key_b64
        or base64.b64encode(b"provider-key").decode("ascii"),
        "user_message": "inspect the host",
    }


def _dispatcher(
    agent_storage: AgentStorage,
    service: FakeAgentService,
) -> RequestDispatcher:
    """Register all Agent handlers against real config storage and a fake turn service."""

    dispatcher = RequestDispatcher()
    register_agent_handlers(dispatcher, agent_storage.api_configs, service)
    return dispatcher


@pytest.mark.parametrize(
    ("repository_code", "expected_code"),
    [
        ("MODEL_API_CONFIG_PERSISTENCE_FAILED", "MODEL_API_CONFIG_PERSISTENCE_FAILED"),
        ("INTERNAL_DATABASE_MARKER", "MODEL_API_CONFIG_PERSISTENCE_FAILED"),
    ],
    ids=["known-code", "unknown-code"],
)
def test_api_config_repository_errors_are_allowlisted_and_redacted(
    agent_storage: AgentStorage,
    monkeypatch: pytest.MonkeyPatch,
    repository_code: str,
    expected_code: str,
) -> None:
    """Expose only stable config errors and never repository diagnostics."""

    async def scenario() -> None:
        def fail_list() -> list[object]:
            raise ApiConfigRepositoryError(repository_code, "secret database marker")

        monkeypatch.setattr(agent_storage.api_configs, "list", fail_list)
        dispatcher = _dispatcher(agent_storage, FakeAgentService(_result()))

        with pytest.raises(DispatchError) as error:
            await dispatcher.dispatch(
                *application_frame(
                    "agent.api_configs.list",
                    params={},
                )
            )

        assert error.value.error_code == expected_code
        assert str(error.value) == "model API configuration persistence failed"
        assert "secret database marker" not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": True},
        {"ssh_session_id": "not-a-uuid"},
        {"api_key_b64": "%%%"},
        {"api_key_b64": "cHJvdmlkZXIta2V5===="},
        {"api_key_b64": base64.b64encode(b"\xff").decode("ascii")},
    ],
    ids=["unknown", "uuid", "base64", "noncanonical", "utf8"],
)
def test_agent_turn_rejects_strict_payload_errors(
    agent_storage: AgentStorage,
    mutation: dict[str, object],
) -> None:
    """Reject unknown fields and malformed identifiers or secret encodings."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        params = _turn_params(config.api_config_id, config.api_key_secret_ref)
        params.update(mutation)
        dispatcher = _dispatcher(agent_storage, FakeAgentService(_result()))

        with pytest.raises(DispatchError) as error:
            await dispatcher.dispatch(
                *application_frame("agent.turn.run", params=params)
            )

        assert error.value.error_code == "INVALID_REQUEST_PAYLOAD"

    asyncio.run(scenario())


def test_agent_turn_rechecks_enabled_config_and_credential_reference(
    agent_storage: AgentStorage,
) -> None:
    """Close the Vault lookup race before provider invocation."""

    async def scenario() -> None:
        disabled = agent_storage.api_configs.create(
            valid_api_config_input().model_copy(update={"enabled": False})
        )
        dispatcher = _dispatcher(agent_storage, FakeAgentService(_result()))
        with pytest.raises(DispatchError) as disabled_error:
            await dispatcher.dispatch(
                *application_frame(
                    "agent.turn.run",
                    params=_turn_params(
                        disabled.api_config_id,
                        disabled.api_key_secret_ref,
                    ),
                )
            )
        assert disabled_error.value.error_code == "MODEL_API_CONFIG_DISABLED"

        enabled = agent_storage.api_configs.create(valid_api_config_input())
        with pytest.raises(DispatchError) as changed_error:
            await dispatcher.dispatch(
                *application_frame(
                    "agent.turn.run",
                    params=_turn_params(enabled.api_config_id, uuid4()),
                )
            )
        assert changed_error.value.error_code == "MODEL_API_CONFIG_CHANGED"

    asyncio.run(scenario())


def test_agent_turn_decodes_key_and_returns_only_turn_result(
    agent_storage: AgentStorage,
) -> None:
    """Pass a short-lived SecretStr to the service and serialize the strict result."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = FakeAgentService(_result())
        dispatcher = _dispatcher(agent_storage, service)
        result = await dispatcher.dispatch(
            *application_frame(
                "agent.turn.run",
                params=_turn_params(
                    config.api_config_id,
                    config.api_key_secret_ref,
                ),
            )
        )

        assert service.api_keys == ["provider-key"]
        assert set(result.payload) == {
            "conversation_id",
            "agent_run_id",
            "status",
            "final_text",
            "react_iteration",
            "error_code",
        }
        assert "api_key" not in str(result.payload)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error_code",
    ["SSH_SESSION_UNAVAILABLE", "COMMAND_REJECTED_DANGEROUS_PATTERN"],
)
def test_agent_service_errors_map_to_safe_dispatch_codes(
    agent_storage: AgentStorage,
    error_code: str,
) -> None:
    """Preserve stable codes for unavailable sessions and safety failures."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        dispatcher = _dispatcher(
            agent_storage,
            FakeAgentService(AgentServiceError(error_code)),
        )

        with pytest.raises(DispatchError) as error:
            await dispatcher.dispatch(
                *application_frame(
                    "agent.turn.run",
                    params=_turn_params(
                        config.api_config_id,
                        config.api_key_secret_ref,
                    ),
                )
            )

        assert error.value.error_code == error_code
        assert error.value.details == {}

    asyncio.run(scenario())


def test_agent_turn_cancellation_flows_through_dispatcher(
    agent_storage: AgentStorage,
) -> None:
    """Use the dispatcher cancellation event and return a durable CANCELLED result."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = FakeAgentService(_result(), wait_for_cancel=True)
        dispatcher = _dispatcher(agent_storage, service)
        request_id = uuid4()
        request = application_frame(
            "agent.turn.run",
            params=_turn_params(
                config.api_config_id,
                config.api_key_secret_ref,
            ),
            request_id=request_id,
        )

        running = asyncio.create_task(dispatcher.dispatch(*request))
        await service.started.wait()
        assert await dispatcher.cancel(request_id) is True
        result = await running

        assert result.payload["status"] == "CANCELLED"
        assert result.payload["error_code"] == "AGENT_CANCELLED"

    asyncio.run(scenario())


def test_unexpected_turn_error_never_exposes_key_or_output_marker(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Map unknown failures without logging or returning sensitive exception text."""

    async def scenario() -> None:
        marker = "remote-output-marker-77"
        key = "provider-key-marker-88"
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = FakeAgentService(RuntimeError(f"{key}:{marker}"))
        dispatcher = _dispatcher(agent_storage, service)
        caplog.set_level(logging.DEBUG)

        with pytest.raises(DispatchError) as error:
            await dispatcher.dispatch(
                *application_frame(
                    "agent.turn.run",
                    params=_turn_params(
                        config.api_config_id,
                        config.api_key_secret_ref,
                        api_key_b64=base64.b64encode(key.encode()).decode("ascii"),
                    ),
                )
            )

        assert error.value.error_code == "AGENT_TURN_FAILED"
        assert error.value.details == {}
        exposed = f"{error.value}:{error.value.details}:{caplog.text}"
        assert key not in exposed
        assert marker not in exposed

    asyncio.run(scenario())


def test_api_config_handlers_round_trip_without_secret_bytes(
    agent_storage: AgentStorage,
) -> None:
    """Expose CRUD metadata and opaque Vault references through normal frames."""

    async def scenario() -> None:
        dispatcher = _dispatcher(agent_storage, FakeAgentService(_result()))
        value = valid_api_config_input()
        created = await dispatcher.dispatch(
            *application_frame(
                "agent.api_configs.create",
                params=value.model_dump(mode="json"),
            )
        )
        config = created.payload["config"]
        config_id = UUID(config["api_config_id"])

        listed = await dispatcher.dispatch(
            *application_frame(
                "agent.api_configs.list",
                params={},
            )
        )
        fetched = await dispatcher.dispatch(
            *application_frame(
                "agent.api_configs.get",
                params={"api_config_id": str(config_id)},
            )
        )

        assert listed.payload["configs"] == [config]
        assert fetched.payload["config"] == config
        assert "provider-key" not in str(listed.payload)

    asyncio.run(scenario())


def test_oversized_agent_response_fails_without_truncating_final_text(
    agent_storage: AgentStorage,
) -> None:
    """Reject a terminal HTTP response exceeding the limit instead of truncating it."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        oversized_text = "x" * (MAX_JSON_RESPONSE_BYTES + 1)
        oversized = AgentTurnResult(
            conversation_id=uuid4(),
            agent_run_id=uuid4(),
            status=AgentRunStatus.COMPLETED,
            final_text=oversized_text,
            react_iteration=0,
            error_code=None,
        )
        dispatcher = _dispatcher(agent_storage, FakeAgentService(oversized))

        with pytest.raises(DispatchError) as error:
            await dispatcher.dispatch(
                *application_frame(
                    "agent.turn.run",
                    params=_turn_params(
                        config.api_config_id,
                        config.api_key_secret_ref,
                    ),
                )
            )

        assert error.value.error_code == "AGENT_RESPONSE_TOO_LARGE"
        exposed = f"{error.value}:{error.value.details}"
        assert oversized_text not in exposed
        assert oversized_text[:1024] not in exposed

    asyncio.run(scenario())
