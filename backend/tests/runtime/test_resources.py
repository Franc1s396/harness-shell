from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness_shell_sidecar.runtime import RuntimeInitializationFailure, RuntimePhase
from harness_shell_sidecar.runtime.resources import RuntimeResources
from harness_shell_sidecar.runtime.settings import RuntimeSettings
from harness_shell_sidecar.storage import RuntimeDatabase


async def discard_event(_event: dict[str, object]) -> None:
    """Provide an asynchronous sink for tests that do not inspect events."""


def settings(tmp_path: Path) -> RuntimeSettings:
    """Return one isolated schema-v6 runtime configuration."""

    return RuntimeSettings.from_data_dir((tmp_path / "runtime-data").resolve())


def test_runtime_models_define_autonomous_lifecycle_boundary() -> None:
    assert [phase.value for phase in RuntimePhase] == [
        "INITIALIZING",
        "READY",
        "DRAINING",
        "CONVERGING",
        "CLOSING",
        "STOPPED",
        "FAILED",
    ]


def test_runtime_resources_initialize_all_handlers_and_shutdown_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        resources = RuntimeResources.initialize_from_settings(
            settings(tmp_path), discard_event
        )

        assert resources.state is RuntimePhase.READY
        for operation in (
            "connections.list",
            "host_key.inspect",
            "ssh.connect",
            "pty.open",
            "manual_sftp.list.begin",
            "agent.api_configs.list",
        ):
            assert resources.dispatcher.handles(operation)
        assert resources.agent_turn_application is not None

        await resources.shutdown()
        await resources.shutdown()
        assert resources.state is RuntimePhase.STOPPED

    asyncio.run(scenario())


def test_runtime_resources_initialization_failure_closes_partial_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_settings = settings(tmp_path)

    def fail_cipher():
        raise OSError("injected cipher failure")

    monkeypatch.setattr(
        "harness_shell_sidecar.runtime.resources.RuntimeCredentialCipher.generate",
        fail_cipher,
    )
    with pytest.raises(RuntimeInitializationFailure, match="initialization failed"):
        RuntimeResources.initialize_from_settings(runtime_settings, discard_event)

    database = RuntimeDatabase.open_plaintext(runtime_settings.database_path)
    database.close()


def test_runtime_resources_preserve_first_cleanup_error_and_run_later_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        resources = RuntimeResources.initialize_from_settings(
            settings(tmp_path), discard_event
        )
        order: list[str] = []

        original_dispatcher_close = resources.dispatcher.close
        original_manual_sftp_close = resources.manual_sftp_service.close_all
        original_ssh_close = resources.ssh_runtime.close_all
        original_database_close = resources.database.close

        async def close_dispatcher() -> None:
            order.append("dispatcher")
            await original_dispatcher_close()

        async def fail_pty() -> None:
            order.append("pty")
            raise OSError("PTY cleanup failed")

        async def close_manual_sftp() -> None:
            order.append("manual_sftp")
            await original_manual_sftp_close()

        async def close_ssh() -> None:
            order.append("ssh")
            await original_ssh_close()

        def close_database() -> None:
            order.append("database")
            original_database_close()

        monkeypatch.setattr(resources.dispatcher, "close", close_dispatcher)
        monkeypatch.setattr(resources.pty_manager, "close_all", fail_pty)
        monkeypatch.setattr(resources.manual_sftp_service, "close_all", close_manual_sftp)
        monkeypatch.setattr(resources.ssh_runtime, "close_all", close_ssh)
        monkeypatch.setattr(resources.database, "close", close_database)

        with pytest.raises(OSError, match="PTY cleanup failed"):
            await resources.shutdown()

        assert order == [
            "dispatcher",
            "pty",
            "manual_sftp",
            "ssh",
            "database",
        ]
        assert resources.state is RuntimePhase.FAILED

    asyncio.run(scenario())
