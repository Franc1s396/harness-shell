from __future__ import annotations

import asyncio
from importlib import import_module
from pathlib import Path

import pytest

from harness_shell_sidecar.runtime import RuntimePhase
from harness_shell_sidecar.runtime.resources import RuntimeResources


async def discard_event(_event: dict[str, object]) -> None:
    """Provide a bounded no-op event sink for local resource tests."""


def load_runtime_settings_type():
    """Load the target settings type so a missing module is a RED failure."""

    try:
        return import_module(
            "harness_shell_sidecar.runtime.settings"
        ).RuntimeSettings
    except (ModuleNotFoundError, AttributeError) as exc:
        raise AssertionError("autonomous Runtime settings are not implemented") from exc


def test_plaintext_resources_initialize_without_injected_keys(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings_type = load_runtime_settings_type()
        settings = settings_type.from_data_dir(tmp_path.resolve())
        resources = RuntimeResources.initialize_from_settings(
            settings,
            discard_event,
        )
        try:
            assert resources.database.path == tmp_path / "runtime.sqlite3"
            assert resources.state is RuntimePhase.READY
            assert resources.credential_cipher.public_key().key_id
            assert resources.credential_repository is not None
            assert resources.dispatcher.handles("ssh.connect")
            assert resources.dispatcher.handles("agent.turn.run")
        finally:
            await resources.shutdown()

        assert resources.state is RuntimePhase.STOPPED

    asyncio.run(scenario())


def test_runtime_settings_require_absolute_data_directory() -> None:
    settings_type = load_runtime_settings_type()

    with pytest.raises(ValueError, match="absolute"):
        settings_type.from_data_dir(Path("relative-runtime"))


def test_runtime_settings_only_publish_consumed_paths(tmp_path: Path) -> None:
    """Do not retain trace or heartbeat settings without a Python consumer."""

    settings_type = load_runtime_settings_type()
    settings = settings_type.from_data_dir(tmp_path.resolve())

    assert settings.data_dir == tmp_path.resolve()
    assert settings.database_path == tmp_path.resolve() / "runtime.sqlite3"
    assert settings.log_dir == tmp_path.resolve() / "logs"
    assert not hasattr(settings, "trace_dir")
    assert not hasattr(settings, "heartbeat_interval_ms")
    assert not hasattr(settings, "heartbeat_timeout_ms")
