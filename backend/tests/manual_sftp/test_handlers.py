"""Strict application contract tests for manual SFTP operations."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp.handlers import (
    ManualSftpApplication,
    register_manual_sftp_handlers,
)
from harness_shell_sidecar.manual_sftp.models import (
    DeletePlanSummary,
    DownloadChunk,
    ManualSftpContext,
    RecoverySummary,
    TransferSnapshot,
    UploadChunkAck,
)
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext


SESSION_ID = UUID("00000000-0000-4000-8000-000000000222")
CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000223")
RECOVERY_ID = UUID("00000000-0000-4000-8000-000000000224")
FRESH_OPERATION_ID = UUID("00000000-0000-4000-8000-000000000225")


class FakeService:
    """Capture validated calls without opening a remote channel."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def open(self, *args, **kwargs):
        self.calls.append(("open", args, kwargs))
        return ManualSftpContext(
            ssh_session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            home="/home/demo",
            host_label=str(CONNECTION_ID),
            sftp_version=3,
        )

    async def upload_chunk(self, *args, **kwargs):
        self.calls.append(("upload_chunk", args, kwargs))
        return UploadChunkAck(
            operation_id=args[0],
            sequence=kwargs["sequence"],
            offset=kwargs["offset"],
            accepted_bytes=len(kwargs["chunk"]),
        )

    async def download_chunk(self, *args, **kwargs):
        self.calls.append(("download_chunk", args, kwargs))
        return DownloadChunk(
            operation_id=args[0],
            sequence=kwargs["sequence"],
            offset=kwargs["offset"],
            data=b"payload",
            next_offset=kwargs["offset"] + 7,
            eof=True,
        )

    async def recovery_execute(self, *args, **kwargs):
        self.calls.append(("recovery_execute", args, kwargs))
        return RecoverySummary(
            recovery_id=RECOVERY_ID,
            operation_id=RECOVERY_ID,
            kind="mutation_unknown",
            host_label="demo-host",
            remote_path="/home/demo/payload.bin",
            display_name="payload.bin",
            state="outcome_unknown",
            created_at="2026-08-29T00:00:00Z",
            available_actions=("keep",),
        )

    async def delete_preflight(self, *args, **kwargs):
        self.calls.append(("delete_preflight", args, kwargs))
        return DeletePlanSummary(
            delete_plan_id=uuid4(),
            operation_id=args[0],
            root_path="/home/demo/tree",
            root_snapshot=TransferSnapshot(
                path="/home/demo/tree",
                exists=True,
                entry_type="directory",
                size=None,
                mtime_ns="1770000000000000000",
                sha256=None,
            ),
            file_count=0,
            directory_count=1,
            symlink_count=0,
            total_byte_count=0,
            manifest_sha256="a" * 64,
            complete=True,
        )


class RetainedStateFailureService(FakeService):
    """Expose one persisted remote uncertainty through the handler boundary."""

    async def open(self, *args, **kwargs):
        raise ManualSftpError(
            "SFTP_MUTATION_OUTCOME_UNKNOWN",
            "The remote mutation outcome could not be confirmed.",
            operation_state="outcome_unknown",
        )


def test_binary_application_keeps_upload_and_download_bytes_unencoded() -> None:
    """Keep Base64 entirely inside the temporary stdio adapter closures."""

    async def scenario() -> None:
        service = FakeService()
        application = ManualSftpApplication(service)
        operation_id = uuid4()
        context = RequestContext(uuid4(), asyncio.Event())

        ack = await application.upload_chunk(
            context,
            operation_id,
            sequence=1,
            offset=0,
            chunk=b"abc",
        )
        assert ack.accepted_bytes == 3
        assert ack.sequence == 1
        assert ack.offset == 0

        chunk = await application.download_chunk(
            context,
            operation_id,
            sequence=2,
            offset=3,
        )
        assert chunk.data == b"payload"
        assert chunk.sequence == 2
        assert chunk.offset == 3
        assert chunk.eof is True

    asyncio.run(scenario())


def test_manual_sftp_open_is_strict_and_returns_typed_context() -> None:
    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        service = FakeService()
        register_manual_sftp_handlers(dispatcher, service)

        result = await dispatcher.dispatch(
            uuid4(), "manual_sftp.open", {"ssh_session_id": str(SESSION_ID)}
        )
        assert result.payload["context"]["home"] == "/home/demo"
        assert service.calls == [("open", (SESSION_ID,), {})]

        with pytest.raises(DispatchError) as raised:
            await dispatcher.dispatch(
                uuid4(),
                "manual_sftp.open",
                {"ssh_session_id": str(SESSION_ID), "unexpected": True},
            )
        assert raised.value.error_code == "INVALID_REQUEST_PAYLOAD"

    asyncio.run(scenario())


def test_manual_sftp_error_preserves_only_safe_operation_state() -> None:
    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        register_manual_sftp_handlers(dispatcher, RetainedStateFailureService())

        with pytest.raises(DispatchError) as raised:
            await dispatcher.dispatch(
                uuid4(), "manual_sftp.open", {"ssh_session_id": str(SESSION_ID)}
            )

        assert raised.value.error_code == "SFTP_MUTATION_OUTCOME_UNKNOWN"
        assert raised.value.details == {"operation_state": "outcome_unknown"}

    asyncio.run(scenario())


def test_manual_sftp_registers_read_and_transfer_families() -> None:
    dispatcher = RequestDispatcher()
    register_manual_sftp_handlers(dispatcher, FakeService())

    expected = {
        "manual_sftp.open",
        "manual_sftp.list.begin",
        "manual_sftp.list.next",
        "manual_sftp.list.close",
        "manual_sftp.lstat",
        "manual_sftp.readlink",
        "manual_sftp.realpath",
        "manual_sftp.sha256",
    }
    assert all(dispatcher.handles(operation) for operation in expected)
    assert dispatcher.handles("manual_sftp.upload.begin") is True
    assert dispatcher.handles("manual_sftp.download.abort") is True
    assert dispatcher.handles("manual_sftp.delete.execute") is True
    assert dispatcher.handles("manual_sftp.recovery.execute") is True
    assert dispatcher.handles("agent_sftp.upload") is False


def test_recovery_execute_forwards_fresh_operation_id() -> None:
    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        service = FakeService()
        register_manual_sftp_handlers(dispatcher, service)
        params = {
            "recovery_id": str(RECOVERY_ID),
            "action": "restore_tombstone",
            "operation_id": str(FRESH_OPERATION_ID),
        }

        result = await dispatcher.dispatch(
            uuid4(), "manual_sftp.recovery.execute", params
        )
        assert result.payload["recovery"]["recovery_id"] == str(RECOVERY_ID)
        assert service.calls == [
            (
                "recovery_execute",
                (RECOVERY_ID, "restore_tombstone", FRESH_OPERATION_ID),
                {},
            )
        ]

        with pytest.raises(DispatchError) as missing:
            await dispatcher.dispatch(
                uuid4(),
                "manual_sftp.recovery.execute",
                {"recovery_id": str(RECOVERY_ID), "action": "restore_tombstone"},
            )
        assert missing.value.error_code == "INVALID_REQUEST_PAYLOAD"

    asyncio.run(scenario())


def test_delete_preflight_forwards_selected_operation_id() -> None:
    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        service = FakeService()
        register_manual_sftp_handlers(dispatcher, service)
        params = {
            "operation_id": str(FRESH_OPERATION_ID),
            "ssh_session_id": str(SESSION_ID),
            "path": "/home/demo/tree",
        }

        result = await dispatcher.dispatch(
            uuid4(), "manual_sftp.delete.preflight", params
        )
        assert result.payload["delete_plan"]["operation_id"] == str(
            FRESH_OPERATION_ID
        )
        assert service.calls == [
            (
                "delete_preflight",
                (FRESH_OPERATION_ID, SESSION_ID, "/home/demo/tree"),
                {},
            )
        ]

        with pytest.raises(DispatchError) as missing:
            await dispatcher.dispatch(
                uuid4(),
                "manual_sftp.delete.preflight",
                {"ssh_session_id": str(SESSION_ID), "path": "/home/demo/tree"},
            )
        assert missing.value.error_code == "INVALID_REQUEST_PAYLOAD"

    asyncio.run(scenario())
