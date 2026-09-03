"""Explicit-only recovery summary tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp import recovery as recovery_module
from harness_shell_sidecar.manual_sftp.models import (
    OperationTerminalProjection,
    TransferSnapshot,
)
from harness_shell_sidecar.manual_sftp.operation_store import RemoteOperationRecord
from harness_shell_sidecar.manual_sftp.recovery import RecoveryManager
from harness_shell_sidecar.manual_sftp.channels import SftpChannelFactory
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry


CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000501")
SESSION_ID = UUID("00000000-0000-4000-8000-000000000502")


class FakeChannels:
    """Resolve only the connection identity needed by recovery mutations."""

    def session_id_for_recovery(self, **identity) -> UUID:
        """Return the explicitly registered session for the expected connection."""

        assert identity == {
            "connection_id": CONNECTION_ID,
            "connection_profile_version": 7,
            "target_host_key_fingerprint": "SHA256:test-target",
            "jump_connection_id": None,
            "jump_profile_version": None,
            "jump_host_key_fingerprint": None,
        }
        return SESSION_ID

    async def open(self, ssh_session_id: UUID):
        """Return the minimal lease shape needed by delete-temp snapshotting."""

        assert ssh_session_id == SESSION_ID
        return FakeLease()


class NoChannels(FakeChannels):
    """Fail if a proven terminal receipt attempts any remote reconciliation."""

    def session_id_for_recovery(self, **identity) -> UUID:
        raise AssertionError("terminal recovery inspection must not resolve a session")


class FakeLease:
    """Expose an opaque client and deterministic close for a patched snapshot read."""

    def __init__(self) -> None:
        """Create the opaque client marker."""

        self.client = object()

    async def close(self) -> None:
        """Close the fake lease without external resources."""


class FakeOperations:
    """Keep authenticated operation records in memory for focused identity tests."""

    def __init__(self, record: RemoteOperationRecord) -> None:
        """Seed the old recovery record and no fresh operation."""

        self.records = {record.operation_id: record}

    def get(self, operation_id: UUID) -> RemoteOperationRecord | None:
        """Resolve one exact operation identity."""

        return self.records.get(operation_id)

    def put(self, record: RemoteOperationRecord) -> None:
        """Persist a complete replacement record."""

        self.records[record.operation_id] = record

    def list_non_terminal(self) -> tuple[RemoteOperationRecord, ...]:
        """Return the seeded non-terminal records."""

        return tuple(
            record
            for record in self.records.values()
            if record.state not in {"succeeded", "failed", "cancelled"}
        )


class FakeMutations:
    """Capture the fresh operation identity without performing remote I/O."""

    def __init__(self) -> None:
        """Start with no mutation calls."""

        self.rename_operation_ids: list[UUID] = []
        self.remove_operation_ids: list[UUID] = []
        self.delete_preflight_operation_ids: list[UUID] = []
        self._delete_operation_id: UUID | None = None

    async def rename(self, *, operation_id: UUID, **_kwargs):
        """Return a trustworthy terminal receipt tied to the supplied identity."""

        self.rename_operation_ids.append(operation_id)
        return OperationTerminalProjection(
            operation_id=operation_id,
            state="succeeded",
            error_code=None,
            message="Restored.",
            sha256=None,
            byte_count=None,
            recovery_id=None,
        )

    async def remove(self, *, operation_id: UUID, **_kwargs):
        """Capture the selected identity for delete-temp."""

        self.remove_operation_ids.append(operation_id)
        return terminal(operation_id, "Temporary file removed.")

    async def delete_preflight(
        self, _session_id: UUID, _path: str, *, operation_id: UUID
    ):
        """Capture the selected identity for continue-delete."""

        self.delete_preflight_operation_ids.append(operation_id)
        self._delete_operation_id = operation_id
        return SimpleNamespace(delete_plan_id=uuid4())

    async def delete_execute(self, _delete_plan_id: UUID):
        """Return the terminal receipt for the preselected recursive-delete identity."""

        assert self._delete_operation_id is not None
        return terminal(self._delete_operation_id, "Delete continued.")


def terminal(operation_id: UUID, message: str) -> OperationTerminalProjection:
    """Build a trustworthy successful receipt for one fake mutation."""

    return OperationTerminalProjection(
        operation_id=operation_id,
        state="succeeded",
        error_code=None,
        message=message,
        sha256=None,
        byte_count=None,
        recovery_id=None,
    )


def recovery_record(
    operation_id: UUID, *, kind: str = "recursive_delete"
) -> RemoteOperationRecord:
    """Build one plaintext-record shape requiring tombstone restoration."""

    return RemoteOperationRecord(
        operation_id=operation_id,
        kind=kind,
        state="cleanup_required",
        connection_id=CONNECTION_ID,
        connection_profile_version=7,
        host_label="demo-host",
        target_host_key_fingerprint="SHA256:test-target",
        jump_connection_id=None,
        jump_profile_version=None,
        jump_host_key_fingerprint=None,
        remote_path="/home/demo/tree",
        temp_path="/home/demo/.tree.tombstone",
        expected_sha256=None,
        target_snapshot=None,
        terminal_receipt=None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def test_recovery_manager_exposes_no_automatic_replay_entrypoint() -> None:
    """Recovery may inspect or execute a new action, never replay an old request."""

    assert not hasattr(RecoveryManager, "replay")
    assert not hasattr(RecoveryManager, "resume_automatically")


def test_recovery_rejects_a_reused_connection_id_with_a_new_profile_version() -> None:
    sessions = SshSessionRegistry()
    sessions.register(
        CONNECTION_ID,
        object(),
        connection_profile_version=8,
        host_label="edited-host",
        target_host_key_fingerprint="SHA256:test-target",
    )

    with pytest.raises(ManualSftpError) as raised:
        SftpChannelFactory(sessions).session_id_for_recovery(
            connection_id=CONNECTION_ID,
            connection_profile_version=7,
            target_host_key_fingerprint="SHA256:test-target",
            jump_connection_id=None,
            jump_profile_version=None,
            jump_host_key_fingerprint=None,
        )

    assert raised.value.error_code == "SFTP_RECOVERY_SESSION_MISMATCH"


def test_recovery_execute_uses_and_validates_the_rust_selected_operation_id() -> None:
    async def scenario() -> None:
        old_operation_id = uuid4()
        fresh_operation_id = uuid4()
        operations = FakeOperations(recovery_record(old_operation_id))
        mutations = FakeMutations()
        recovery = RecoveryManager(FakeChannels(), operations, mutations)

        terminal = await recovery.execute(
            old_operation_id,
            "restore_tombstone",
            fresh_operation_id,
        )
        assert terminal.operation_id == fresh_operation_id
        assert mutations.rename_operation_ids == [fresh_operation_id]

        with pytest.raises(ManualSftpError, match="SFTP_OPERATION_DUPLICATE"):
            await recovery.execute(
                old_operation_id,
                "restore_tombstone",
                old_operation_id,
            )

    asyncio.run(scenario())


def test_all_mutating_recovery_actions_use_the_rust_selected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete-temp, continue-delete, and restore must use exactly the wire identity."""

    async def fake_snapshot(
        _client: object, path: str, *, include_hash: bool
    ) -> TransferSnapshot:
        assert include_hash
        return TransferSnapshot(
            path=path,
            exists=True,
            entry_type="file",
            size=1,
            mtime_ns="1770000000000000000",
            sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    monkeypatch.setattr(recovery_module, "_snapshot", fake_snapshot)

    async def scenario() -> None:
        for action, kind, capture_name in (
            ("delete_temp", "upload", "remove_operation_ids"),
            ("continue_delete", "recursive_delete", "delete_preflight_operation_ids"),
            ("restore_tombstone", "recursive_delete", "rename_operation_ids"),
        ):
            old_operation_id = uuid4()
            fresh_operation_id = uuid4()
            mutations = FakeMutations()
            recovery = RecoveryManager(
                FakeChannels(),
                FakeOperations(recovery_record(old_operation_id, kind=kind)),
                mutations,
            )

            result = await recovery.execute(
                old_operation_id,
                action,
                fresh_operation_id,
            )

            assert result.operation_id == fresh_operation_id
            assert getattr(mutations, capture_name) == [fresh_operation_id]

    asyncio.run(scenario())


def test_read_only_reconciliation_persists_the_proven_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proven upload target must disappear from future recovery listings."""

    expected_hash = "a" * 64

    async def fake_snapshot(
        _client: object, path: str, *, include_hash: bool
    ) -> TransferSnapshot:
        assert path == "/home/demo/tree"
        assert include_hash
        return TransferSnapshot(
            path=path,
            exists=True,
            entry_type="file",
            size=1,
            mtime_ns="1770000000000000000",
            sha256=expected_hash,
        )

    monkeypatch.setattr(recovery_module, "_snapshot", fake_snapshot)

    async def scenario() -> None:
        operation_id = uuid4()
        record = recovery_record(operation_id, kind="upload").model_copy(
            update={"expected_sha256": expected_hash}
        )
        operations = FakeOperations(record)
        recovery = RecoveryManager(FakeChannels(), operations, FakeMutations())

        result = await recovery.inspect(operation_id)

        assert isinstance(result, OperationTerminalProjection)
        assert result.state == "succeeded"
        persisted = operations.get(operation_id)
        assert persisted is not None
        assert persisted.state == "succeeded"
        assert persisted.terminal_receipt == result
        assert recovery.list() == ()

    asyncio.run(scenario())


def test_late_terminal_recovery_inspect_returns_the_persisted_receipt() -> None:
    async def scenario() -> None:
        operation_id = uuid4()
        receipt = terminal(operation_id, "Late mutation result confirmed.")
        record = recovery_record(operation_id).model_copy(
            update={"state": "succeeded", "terminal_receipt": receipt}
        )
        recovery = RecoveryManager(NoChannels(), FakeOperations(record), FakeMutations())

        result = await recovery.inspect(operation_id)

        assert result == receipt

    asyncio.run(scenario())
