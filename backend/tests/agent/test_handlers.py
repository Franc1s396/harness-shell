from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from harness_shell_sidecar.agent.api_configs import ApiConfigRepositoryError
from harness_shell_sidecar.agent.contracts import (
    AgentRunStatus,
    AgentTurnInput,
    AgentTurnResult,
)
from harness_shell_sidecar.agent.handlers import register_agent_handlers
from harness_shell_sidecar.agent.service import AgentServiceError
from harness_shell_sidecar.credentials import (
    CredentialRepository,
    RuntimeCredentialCipher,
)
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.storage import PlaintextRecord

from .conftest import AgentStorage, valid_api_config_input
from .fakes import RecordingTurnSink


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
        event_sink: object,
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
) -> dict[str, object]:
    """Build the identity-only public turn payload."""

    return {
        "conversation_id": None,
        "ssh_session_id": str(uuid4()),
        "api_config_id": str(api_config_id),
        "user_message": "inspect the host",
    }


def _dispatcher(
    agent_storage: AgentStorage,
    service: FakeAgentService,
    cipher: RuntimeCredentialCipher | None = None,
) -> RequestDispatcher:
    """Register all Agent handlers against real config storage and a fake turn service."""

    dispatcher = RequestDispatcher()
    credential_ids = agent_storage.database.execute(
        "SELECT api_key_credential_id FROM model_api_configs"
    ).fetchall()
    for (credential_id_text,) in credential_ids:
        credential_id = UUID(credential_id_text)
        if agent_storage.record_store.get("credential", str(credential_id)) is None:
            payload = json.dumps(
                {
                    "credential_id": str(credential_id),
                    "kind": "api_key",
                    "secret": "provider-key",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            agent_storage.record_store.put(
                PlaintextRecord("credential", str(credential_id), 1, payload)
            )
    register_agent_handlers(
        dispatcher,
        agent_storage.api_configs,
        service,
        CredentialRepository(agent_storage.record_store),
        cipher or RuntimeCredentialCipher.generate(),
        agent_storage.database,
    )
    return dispatcher


def _registered(
    agent_storage: AgentStorage,
    service: FakeAgentService,
) -> tuple[RequestDispatcher, object]:
    """Return both the shared dispatcher and explicit turn application."""

    dispatcher = RequestDispatcher()
    config_rows = agent_storage.database.execute(
        "SELECT api_key_credential_id FROM model_api_configs"
    ).fetchall()
    for (credential_id_text,) in config_rows:
        credential_id = UUID(credential_id_text)
        payload = json.dumps(
            {
                "credential_id": str(credential_id),
                "kind": "api_key",
                "secret": "provider-key",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        agent_storage.record_store.put(
            PlaintextRecord("credential", str(credential_id), 1, payload)
        )
    application = register_agent_handlers(
        dispatcher,
        agent_storage.api_configs,
        service,
        CredentialRepository(agent_storage.record_store),
        RuntimeCredentialCipher.generate(),
        agent_storage.database,
    )
    return dispatcher, application


def test_turn_uses_explicit_application_inside_dispatcher_ownership(
    agent_storage: AgentStorage,
) -> None:
    """Keep streaming turns out of the buffered JSON dispatcher handler map."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = FakeAgentService(_result())
        dispatcher, application = _registered(agent_storage, service)

        assert application is not None
        assert dispatcher.handles("agent.turn.run") is False
        await dispatcher.execute(
            uuid4(),
            lambda context: application.run(
                context,
                _turn_params(config.api_config_id),
                RecordingTurnSink(),
            ),
        )

        assert service.api_keys == ["provider-key"]

    asyncio.run(scenario())


def encrypted_api_key(
    cipher: RuntimeCredentialCipher,
    secret: str,
) -> dict[str, object]:
    """Encrypt one API key for direct dispatcher aggregate tests."""

    public_key = cipher.public_key()
    aes_key = bytes(range(32))
    iv = bytes(range(12))
    aad = f"harness-shell-credential-v1\0{public_key.key_id}".encode()
    ciphertext = AESGCM(aes_key).encrypt(iv, secret.encode(), aad)
    rsa_public_key = serialization.load_der_public_key(
        base64.b64decode(public_key.public_key_spki_b64, validate=True)
    )
    wrapped_key = rsa_public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "version": 1,
        "key_id": str(public_key.key_id),
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "iv_b64": base64.b64encode(iv).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


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
        params = _turn_params(config.api_config_id)
        params.update(mutation)
        dispatcher, application = _registered(
            agent_storage,
            FakeAgentService(_result()),
        )

        with pytest.raises(DispatchError) as error:
            await dispatcher.execute(
                uuid4(),
                lambda context: application.run(
                    context,
                    params,
                    RecordingTurnSink(),
                ),
            )

        assert error.value.error_code == "INVALID_REQUEST_PAYLOAD"

    asyncio.run(scenario())


def test_agent_turn_rechecks_enabled_config_and_credential_reference(
    agent_storage: AgentStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close the configuration race before provider invocation."""

    async def scenario() -> None:
        disabled = agent_storage.api_configs.create(
            valid_api_config_input().model_copy(update={"enabled": False})
        )
        dispatcher, application = _registered(
            agent_storage,
            FakeAgentService(_result()),
        )
        with pytest.raises(DispatchError) as disabled_error:
            await dispatcher.execute(
                uuid4(),
                lambda context: application.run(
                    context,
                    _turn_params(disabled.api_config_id),
                    RecordingTurnSink(),
                )
            )
        assert disabled_error.value.error_code == "MODEL_API_CONFIG_DISABLED"

        enabled = agent_storage.api_configs.create(valid_api_config_input())
        dispatcher, application = _registered(
            agent_storage,
            FakeAgentService(_result()),
        )
        real_get = agent_storage.api_configs.get
        get_count = 0

        def changed_get(api_config_id: UUID):
            nonlocal get_count
            get_count += 1
            config = real_get(api_config_id)
            if get_count == 2 and config is not None:
                return config.model_copy(update={"model": "changed-model"})
            return config

        monkeypatch.setattr(agent_storage.api_configs, "get", changed_get)
        with pytest.raises(DispatchError) as changed_error:
            await dispatcher.execute(
                uuid4(),
                lambda context: application.run(
                    context,
                    _turn_params(enabled.api_config_id),
                    RecordingTurnSink(),
                )
            )
        assert changed_error.value.error_code == "MODEL_API_CONFIG_CHANGED"

    asyncio.run(scenario())


def test_agent_turn_decodes_key_without_returning_a_json_result(
    agent_storage: AgentStorage,
) -> None:
    """Pass a short-lived SecretStr while leaving output ownership to the sink."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = FakeAgentService(_result())
        dispatcher, application = _registered(agent_storage, service)
        result = await dispatcher.execute(
            uuid4(),
            lambda context: application.run(
                context,
                _turn_params(config.api_config_id),
                RecordingTurnSink(),
            ),
        )

        assert service.api_keys == ["provider-key"]
        assert result is None

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
        dispatcher, application = _registered(
            agent_storage,
            FakeAgentService(AgentServiceError(error_code)),
        )

        with pytest.raises(DispatchError) as error:
            await dispatcher.execute(
                uuid4(),
                lambda context: application.run(
                    context,
                    _turn_params(config.api_config_id),
                    RecordingTurnSink(),
                )
            )

        assert error.value.error_code == error_code
        assert error.value.details == {}

    asyncio.run(scenario())


def test_agent_turn_shutdown_cancellation_flows_through_dispatcher(
    agent_storage: AgentStorage,
) -> None:
    """Use shutdown cancellation and return a durable CANCELLED result."""

    async def scenario() -> None:
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = FakeAgentService(_result(), wait_for_cancel=True)
        dispatcher, application = _registered(agent_storage, service)
        request_id = uuid4()

        running = asyncio.create_task(
            dispatcher.execute(
                request_id,
                lambda context: application.run(
                    context,
                    _turn_params(config.api_config_id),
                    RecordingTurnSink(),
                ),
            )
        )
        await service.started.wait()
        await dispatcher.close()
        result = await running

        assert result is None

    asyncio.run(scenario())


def test_unexpected_turn_error_never_exposes_key_or_output_marker(
    agent_storage: AgentStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Map unknown failures without logging or returning sensitive exception text."""

    async def scenario() -> None:
        marker = "remote-output-marker-77"
        key = "provider-key"
        config = agent_storage.api_configs.create(valid_api_config_input())
        service = FakeAgentService(RuntimeError(f"{key}:{marker}"))
        dispatcher, application = _registered(agent_storage, service)
        caplog.set_level(logging.DEBUG)

        with pytest.raises(DispatchError) as error:
            await dispatcher.execute(
                uuid4(),
                lambda context: application.run(
                    context,
                    _turn_params(config.api_config_id),
                    RecordingTurnSink(),
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
    """Create aggregate credentials while exposing only references in results."""

    async def scenario() -> None:
        cipher = RuntimeCredentialCipher.generate()
        dispatcher = _dispatcher(
            agent_storage,
            FakeAgentService(_result()),
            cipher,
        )
        value = valid_api_config_input()
        params = value.model_dump(mode="json", exclude={"api_key_credential_id"})
        params["api_key_envelope"] = encrypted_api_key(cipher, "provider-key")
        created = await dispatcher.dispatch(
            *application_frame(
                "agent.api_configs.create",
                params=params,
            )
        )
        config = created["config"]

        listed = await dispatcher.dispatch(
            *application_frame(
                "agent.api_configs.list",
                params={},
            )
        )
        assert listed["configs"] == [config]
        assert "provider-key" not in str(listed)

    asyncio.run(scenario())


def test_application_never_serializes_a_service_result(
    agent_storage: AgentStorage,
) -> None:
    """Keep even a large fake service result out of the application return value."""

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
        dispatcher, application = _registered(
            agent_storage,
            FakeAgentService(oversized),
        )

        result = await dispatcher.execute(
            uuid4(),
            lambda context: application.run(
                context,
                _turn_params(config.api_config_id),
                RecordingTurnSink(),
            ),
        )

        assert result is None
        assert oversized_text not in str(result)

    asyncio.run(scenario())
