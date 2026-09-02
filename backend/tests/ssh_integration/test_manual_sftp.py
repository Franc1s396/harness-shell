from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp.models import RemoteEntry, TransferSnapshot
from harness_shell_sidecar.manual_sftp.service import ManualSftpService


SFTP_CHUNK_BYTES = 256 * 1024


async def _ignore_event(_payload: dict) -> None:
    """Accept safe progress events while integration tests inspect remote state."""

    _write_event_evidence(_payload)


def _write_event_evidence(payload: dict) -> None:
    """Append typed event evidence only when the automated gate requests it."""

    evidence_path = os.environ.get("HARNESS_MANUAL_SFTP_EVENT_EVIDENCE")
    if evidence_path is None:
        return
    with Path(evidence_path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _service(runtime_context) -> ManualSftpService:
    """Build a manual-SFTP owner over the fixture's live SSH session registry."""

    return ManualSftpService(
        runtime_context.runtime.sessions,
        runtime_context.records,
        _ignore_event,
    )


def _snapshot(entry: RemoteEntry, *, sha256: str | None = None) -> TransferSnapshot:
    """Project no-follow metadata into the mutation compare snapshot."""

    return TransferSnapshot(
        path=entry.path,
        exists=True,
        entry_type=entry.entry_type,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        sha256=sha256,
    )


async def _upload(
    service: ManualSftpService,
    session_id: UUID,
    remote_path: str,
    payload: bytes,
) -> None:
    """Upload one payload through the complete preflight/chunk/commit sequence."""

    target = await service.upload_preflight(session_id, remote_path)
    operation_id = uuid4()
    await service.upload_begin(
        operation_id=operation_id,
        ssh_session_id=session_id,
        path=remote_path,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_byte_count=len(payload),
        target_snapshot=target,
    )
    offset = 0
    sequence = 0
    while offset < len(payload):
        chunk = payload[offset : offset + SFTP_CHUNK_BYTES]
        acknowledgement = await service.upload_chunk(
            operation_id,
            sequence=sequence,
            offset=offset,
            chunk=chunk,
        )
        assert acknowledgement.operation_id == operation_id
        assert acknowledgement.sequence == sequence
        assert acknowledgement.offset == offset
        assert acknowledgement.accepted_bytes == len(chunk)
        sequence += 1
        offset += acknowledgement.accepted_bytes
    terminal = await service.upload_finish(operation_id)
    assert terminal.state == "succeeded"
    assert terminal.sha256 == hashlib.sha256(payload).hexdigest()
    assert terminal.byte_count == len(payload)


async def _download(
    service: ManualSftpService,
    session_id: UUID,
    remote_path: str,
) -> bytes:
    """Download one complete remote file and verify the terminal digest."""

    operation_id = uuid4()
    ready = await service.download_begin(
        operation_id=operation_id,
        ssh_session_id=session_id,
        path=remote_path,
    )
    payload = bytearray()
    sequence = 0
    offset = 0
    while offset < ready.byte_count:
        chunk = await service.download_chunk(
            operation_id,
            sequence=sequence,
            offset=offset,
        )
        assert chunk.operation_id == operation_id
        assert chunk.sequence == sequence
        assert chunk.offset == offset
        assert chunk.next_offset == offset + len(chunk.data)
        payload.extend(chunk.data)
        sequence += 1
        offset = chunk.next_offset
        if chunk.eof:
            break
    terminal = await service.download_finish(operation_id)
    downloaded = bytes(payload)
    assert terminal.state == "succeeded"
    assert terminal.sha256 == hashlib.sha256(downloaded).hexdigest()
    assert terminal.byte_count == len(downloaded)
    return downloaded


async def _write_external(runtime_context, session_id: UUID, path: str, data: bytes) -> None:
    """Simulate a second SFTP client changing a target in the accepted race model."""

    owner = runtime_context.runtime.sessions.get(session_id)
    assert owner is not None
    async with owner.connection.start_sftp_client() as client:
        async with await client.open(path.encode("utf-8"), "wb") as remote_file:
            await remote_file.write(data)


async def _delete_tree(
    service: ManualSftpService, session_id: UUID, remote_path: str
) -> None:
    """Delete one integration tree through the encrypted manifest/tombstone flow."""

    plan = await service.delete_preflight(uuid4(), session_id, remote_path)
    terminal = await service.delete_execute(plan.delete_plan_id)
    assert terminal.state == "succeeded"


@pytest.mark.parametrize("connector_fixture", ["connect_direct", "connect_proxy"])
def test_manual_sftp_real_openssh_round_trip_and_safety_contracts(
    connector_fixture: str,
    request: pytest.FixtureRequest,
    runtime_context,
) -> None:
    async def scenario() -> None:
        connector: Callable[[], Awaitable[tuple]] = request.getfixturevalue(
            connector_fixture
        )
        connected = await connector()
        status = connected[-1]
        assert status.session_id is not None
        session_id = status.session_id
        service = _service(runtime_context)
        context = await service.open(session_id)
        root = f"{context.home}/manual-sftp-{uuid4().hex}"
        root_created = False
        try:
            mkdir = await service.mkdir(
                operation_id=uuid4(),
                ssh_session_id=session_id,
                parent_path=context.home,
                name=root.rsplit("/", 1)[-1],
            )
            assert mkdir.state == "succeeded"
            root_created = True

            zero_path = f"{root}/zero-byte.txt"
            utf8_path = f"{root}/space 中文 🙂.txt"
            large_path = f"{root}/large.bin"
            await _upload(service, session_id, zero_path, b"")
            utf8_payload = "manual-sftp-内容-🙂".encode()
            await _upload(service, session_id, utf8_path, utf8_payload)
            large_payload = bytes(range(256)) * 2_401
            await _upload(service, session_id, large_path, large_payload)

            assert await _download(service, session_id, zero_path) == b""
            assert await _download(service, session_id, utf8_path) == utf8_payload
            assert await _download(service, session_id, large_path) == large_payload

            overwrite_payload = b"overwritten-with-mandatory-sha256"
            await _upload(service, session_id, utf8_path, overwrite_payload)
            remote_hash = await service.sha256(session_id, utf8_path)
            assert remote_hash.sha256 == hashlib.sha256(overwrite_payload).hexdigest()

            listing = await service.list_begin(session_id, root)
            listed_names = {entry.name for entry in listing.entries}
            assert {"zero-byte.txt", "space 中文 🙂.txt", "large.bin"} <= listed_names
            if not listing.done:
                await service.list_close(listing.listing_id)

            owner = runtime_context.runtime.sessions.get(session_id)
            assert owner is not None
            link_path = f"{root}/payload-link"
            async with owner.connection.start_sftp_client() as client:
                await client.symlink(utf8_path.encode("utf-8"), link_path.encode("utf-8"))
            link = await service.readlink(session_id, link_path)
            assert link.entry_type == "symlink"
            assert link.link_target == utf8_path
            resolved = await service.realpath(session_id, link_path)
            assert resolved.path == utf8_path
            removed_link = await service.remove(
                operation_id=uuid4(),
                ssh_session_id=session_id,
                path=link_path,
                expected_snapshot=_snapshot(link),
            )
            assert removed_link.state == "succeeded"
            assert (await service.lstat(session_id, utf8_path)).entry_type == "file"

            stale_target = f"{root}/changed-before-commit.txt"
            await _upload(service, session_id, stale_target, b"initial")
            frozen = await service.upload_preflight(session_id, stale_target)
            operation_id = uuid4()
            replacement = b"replacement"
            await service.upload_begin(
                operation_id=operation_id,
                ssh_session_id=session_id,
                path=stale_target,
                source_sha256=hashlib.sha256(replacement).hexdigest(),
                source_byte_count=len(replacement),
                target_snapshot=frozen,
            )
            await service.upload_chunk(
                operation_id,
                sequence=0,
                offset=0,
                chunk=replacement,
            )
            await _write_external(
                runtime_context,
                session_id,
                stale_target,
                b"external-client-change",
            )
            with pytest.raises(ManualSftpError, match="SFTP_TARGET_CHANGED"):
                await service.upload_finish(operation_id)
            assert await _download(service, session_id, stale_target) == b"external-client-change"

            nested = f"{root}/nested"
            assert (
                await service.mkdir(
                    operation_id=uuid4(),
                    ssh_session_id=session_id,
                    parent_path=root,
                    name="nested",
                )
            ).state == "succeeded"
            await _upload(service, session_id, f"{nested}/inside.txt", b"inside")
            async with owner.connection.start_sftp_client() as client:
                await client.symlink(
                    utf8_path.encode("utf-8"),
                    f"{nested}/inside-link".encode("utf-8"),
                )
            plan = await service.delete_preflight(uuid4(), session_id, nested)
            assert plan.file_count == 1
            assert plan.directory_count == 1
            assert plan.symlink_count == 1
            assert plan.total_byte_count == len(b"inside")
            assert plan.complete is True
            assert (
                await service.delete_execute(plan.delete_plan_id)
            ).state == "succeeded"

            interrupted_path = f"{root}/interrupted.txt"
            interrupted = await service.upload_preflight(session_id, interrupted_path)
            interrupted_id = uuid4()
            ready = await service.upload_begin(
                operation_id=interrupted_id,
                ssh_session_id=session_id,
                path=interrupted_path,
                source_sha256=hashlib.sha256(b"partial").hexdigest(),
                source_byte_count=len(b"partial"),
                target_snapshot=interrupted,
            )
            await service.upload_chunk(
                interrupted_id,
                sequence=0,
                offset=0,
                chunk=b"partial",
            )
            restarted = _service(runtime_context)
            recoveries = restarted.list_recoveries()
            recovery = next(item for item in recoveries if item.recovery_id == interrupted_id)
            assert recovery.kind == "upload_temp"
            assert recovery.remote_path == ready.temp_path
            inspected = await restarted.recovery_inspect(interrupted_id)
            assert inspected.operation_id == interrupted_id
            assert (await service.upload_abort(interrupted_id)).state == "cancelled"
            await restarted.close_all()
        finally:
            if root_created:
                await _delete_tree(service, session_id, root)
            await service.close_all()

    async def bounded_scenario() -> None:
        """Fail the real-I/O scenario instead of allowing an unbounded hang."""

        try:
            async with asyncio.timeout(30):
                await scenario()
        finally:
            async with asyncio.timeout(10):
                await runtime_context.close()

    asyncio.run(bounded_scenario())


def test_manual_sftp_real_openssh_permission_and_cross_device_fail_closed(
    runtime_context,
    connect_proxy,
    lab,
) -> None:
    async def scenario() -> None:
        _jump, _target, status = await connect_proxy()
        assert status.session_id is not None
        session_id = status.session_id
        service = _service(runtime_context)
        context = await service.open(session_id)
        source_path = f"{context.home}/cross-device-{uuid4().hex}.txt"
        target_path = f"{lab.target_cross_device_root}/{uuid4().hex}.txt"
        try:
            with pytest.raises(ManualSftpError, match="SFTP_PERMISSION_DENIED"):
                await service.mkdir(
                    operation_id=uuid4(),
                    ssh_session_id=session_id,
                    parent_path=lab.target_permission_denied_root,
                    name="must-not-exist",
                )

            await _upload(service, session_id, source_path, b"cross-device-source")
            source_hash = await service.sha256(session_id, source_path)
            with pytest.raises(
                ManualSftpError,
                match="SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED",
            ):
                await service.rename(
                    operation_id=uuid4(),
                    ssh_session_id=session_id,
                    source_path=source_path,
                    target_path=target_path,
                    overwrite=False,
                    source_snapshot=source_hash.snapshot,
                    target_snapshot=None,
                )
            assert (await service.lstat(session_id, source_path)).entry_type == "file"
            with pytest.raises(Exception):
                await service.lstat(session_id, target_path)
        finally:
            await service.close_all()

    async def bounded_scenario() -> None:
        """Fail the injected-error scenario instead of allowing an unbounded hang."""

        try:
            async with asyncio.timeout(30):
                await scenario()
        finally:
            async with asyncio.timeout(10):
                await runtime_context.close()

    asyncio.run(bounded_scenario())
