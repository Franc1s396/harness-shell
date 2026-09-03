"""No-follow manual SFTP mutations, tombstones, and recursive delete plans."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import posixpath
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncssh
from asyncssh.constants import FXR_ATOMIC, FXR_OVERWRITE

from .channels import SftpChannelFactory, SftpChannelLease
from .errors import ManualSftpError, map_typed_sftp_status
from .listings import remote_entry
from .models import (
    DeleteManifestEntry,
    DeletePlanSummary,
    MutationProgressProjection,
    OperationTerminalProjection,
    TransferSnapshot,
)
from .operation_store import (
    DeletePlanRecord,
    ManualSftpOperationStore,
    RemoteOperationRecord,
)
from .paths import join_remote_path, validate_basename, validate_remote_path
from .transfers import _snapshot


MAX_MANIFEST_ENTRIES = 50_000
NO_PROGRESS_TIMEOUT_SECONDS = 60
MUTATION_REQUEST_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    """Pair canonical projected manifest metadata with the actual remote path."""

    #: Canonical entry used for hashing and plaintext persistence.
    manifest: DeleteManifestEntry
    #: Actual path used only by the current in-memory operation.
    actual_path: str


@dataclass(frozen=True, slots=True)
class _ManifestScan:
    """Complete no-follow scan and its deterministic digest/counts."""

    #: Entries ordered by canonical UTF-8 path bytes.
    entries: tuple[_ScannedEntry, ...]
    #: SHA-256 of sorted compact JSON Lines records.
    sha256: str
    #: Regular file count.
    file_count: int
    #: Directory count including root.
    directory_count: int
    #: Symbolic-link count without targets.
    symlink_count: int
    #: Sum of known regular-file sizes.
    total_byte_count: int


@dataclass(slots=True)
class _DeletePlanState:
    """Keep the non-persisted live SSH binding required to execute one plan."""

    #: Live SSH session selected during preflight; never persisted.
    ssh_session_id: UUID
    #: Encrypted canonical plan record without a live session ID.
    record: DeletePlanRecord


class MutationManager:
    """Own strict single-dispatch mutations and one-shot recursive-delete plans."""

    def __init__(
        self,
        channels: SftpChannelFactory,
        operations: ManualSftpOperationStore,
        event_listener: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Bind channel, plaintext state, and safe progress projection owners."""

        self._channels = channels
        self._operations = operations
        self._event_listener = event_listener
        self._delete_plans: dict[UUID, _DeletePlanState] = {}

    async def mkdir(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        parent_path: str,
        name: str,
    ) -> OperationTerminalProjection:
        """Create one absent child directory exactly once."""

        target_path = join_remote_path(parent_path, validate_basename(name))
        self._require_new_operation(operation_id)
        lease = await self._channels.open(ssh_session_id)
        try:
            if (await _snapshot(lease.client, target_path, include_hash=False)).exists:
                raise ManualSftpError(
                    "SFTP_TARGET_EXISTS", "The remote directory already exists."
                )
            # A target-exists result proves no mutation was attempted, so persist intent only
            # after this deterministic precondition succeeds.
            record = self._operation_record(
                operation_id, "mkdir", lease, target_path, None, None
            )
            self._operations.put(record)
            try:
                await _mutation_request(
                    lease.client.mkdir(target_path.encode("utf-8"))
                )
            except asyncssh.SFTPPermissionDenied as exc:
                raise self._terminal_failure(
                    record,
                    "SFTP_PERMISSION_DENIED",
                    "The server denied the remote directory creation.",
                ) from exc
            except (
                asyncssh.SFTPFileAlreadyExists,
                asyncssh.SFTPNoSuchFile,
                asyncssh.SFTPNoSuchPath,
            ) as exc:
                error = map_typed_sftp_status(exc)
                assert error is not None
                raise self._terminal_failure(
                    record, error.error_code, error.safe_message
                ) from exc
            except Exception as exc:
                return await self._mutation_unknown(record, lease, exc)
            receipt = _terminal(
                operation_id, "succeeded", None, "The remote directory was created."
            )
            self._put_terminal(record, "succeeded", receipt)
            return receipt
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote directory creation.",
            ) from exc
        finally:
            await lease.close()

    async def rename(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        source_path: str,
        target_path: str,
        overwrite: bool,
        source_snapshot: TransferSnapshot | None = None,
        target_snapshot: TransferSnapshot | None = None,
    ) -> OperationTerminalProjection:
        """Revalidate both names and issue one atomic rename without fallback."""

        source = validate_remote_path(source_path)
        target = validate_remote_path(target_path)
        self._require_new_operation(operation_id)
        lease = await self._channels.open(ssh_session_id)
        try:
            observed_source = await _snapshot(
                lease.client, source, include_hash=source_snapshot is not None
            )
            if not observed_source.exists:
                raise ManualSftpError(
                    "SFTP_SOURCE_NOT_FOUND", "The remote source does not exist."
                )
            observed_target = await _snapshot(
                lease.client, target, include_hash=target_snapshot is not None
            )
            if source_snapshot is not None and observed_source != source_snapshot:
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED", "The remote source changed before rename."
                )
            if target_snapshot is not None and observed_target != target_snapshot:
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED", "The remote target changed before rename."
                )
            if observed_target.exists and not overwrite:
                raise ManualSftpError(
                    "SFTP_TARGET_EXISTS", "The remote rename target already exists."
                )
            source_fsid = await _filesystem_id(lease.client, source)
            target_fsid = await _filesystem_id(
                lease.client, posixpath.dirname(target) or "/"
            )
            if (
                source_fsid is not None
                and target_fsid is not None
                and source_fsid != target_fsid
            ):
                raise ManualSftpError(
                    "SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED",
                    "Cross-device remote moves are not supported.",
                )
            record = self._operation_record(
                operation_id,
                "rename",
                lease,
                target,
                source,
                observed_target,
            )
            self._operations.put(record)
            # On v3, any non-zero flag selects OpenSSH posix-rename and can overwrite an
            # entry created after the absent-target snapshot. Use standard no-clobber
            # rename for absent targets; v5+ has explicit atomic no-overwrite flags.
            flags = (
                FXR_ATOMIC | FXR_OVERWRITE
                if observed_target.exists
                else (FXR_ATOMIC if lease.client.version >= 5 else 0)
            )
            try:
                await _mutation_request(
                    lease.client.rename(
                        source.encode("utf-8"), target.encode("utf-8"), flags=flags
                    )
                )
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    receipt = _terminal(
                        operation_id,
                        "failed",
                        "SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED",
                        "Cross-device remote moves are not supported.",
                    )
                    self._put_terminal(record, "failed", receipt)
                    raise ManualSftpError(
                        "SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED",
                        "Cross-device remote moves are not supported.",
                    ) from exc
                return await self._mutation_unknown(record, lease, exc)
            except (
                asyncssh.SFTPFileAlreadyExists,
                asyncssh.SFTPNoSuchFile,
                asyncssh.SFTPNoSuchPath,
            ) as exc:
                error = map_typed_sftp_status(
                    exc,
                    exists_code="SFTP_TARGET_CHANGED",
                )
                assert error is not None
                raise self._terminal_failure(
                    record, error.error_code, error.safe_message
                ) from exc
            except asyncssh.SFTPPermissionDenied as exc:
                raise self._terminal_failure(
                    record,
                    "SFTP_PERMISSION_DENIED",
                    "The server denied the remote rename.",
                ) from exc
            except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
                error = map_typed_sftp_status(exc)
                assert error is not None
                raise self._terminal_failure(
                    record, error.error_code, error.safe_message
                ) from exc
            except asyncssh.SFTPOpUnsupported as exc:
                receipt = _terminal(
                    operation_id,
                    "failed",
                    "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
                    "The server does not support the required atomic rename.",
                )
                self._put_terminal(record, "failed", receipt)
                raise ManualSftpError(
                    "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
                    "The server does not support the required atomic rename.",
                ) from exc
            except Exception as exc:
                return await self._mutation_unknown(record, lease, exc)
            receipt = _terminal(
                operation_id, "succeeded", None, "The remote entry was renamed."
            )
            self._put_terminal(record, "succeeded", receipt)
            return receipt
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED", "The server denied the remote rename."
            ) from exc
        finally:
            await lease.close()

    async def remove(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        path: str,
        expected_snapshot: TransferSnapshot,
    ) -> OperationTerminalProjection:
        """Remove one unchanged file/link or one proven-empty directory."""

        remote_path = validate_remote_path(path)
        self._require_new_operation(operation_id)
        lease = await self._channels.open(ssh_session_id)
        try:
            current = await _snapshot(
                lease.client, remote_path, include_hash=expected_snapshot.entry_type == "file"
            )
            if current != expected_snapshot:
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED", "The remote entry changed before removal."
                )
            if current.entry_type == "directory":
                iterator = lease.client.scandir(remote_path.encode("utf-8"))
                try:
                    while True:
                        try:
                            entry = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        if _decode_name(entry.filename) not in {".", ".."}:
                            raise ManualSftpError(
                                "SFTP_DIRECTORY_NOT_EMPTY",
                                "The remote directory is not empty.",
                            )
                finally:
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close()
            # Directory emptiness is a deterministic pre-dispatch check. Only persist intent
            # after it succeeds, immediately before the one remove/rmdir call.
            record = self._operation_record(
                operation_id, "remove", lease, remote_path, None, expected_snapshot
            )
            self._operations.put(record)
            try:
                if current.entry_type == "directory":
                    await _mutation_request(
                        lease.client.rmdir(remote_path.encode("utf-8"))
                    )
                else:
                    await _mutation_request(
                        lease.client.remove(remote_path.encode("utf-8"))
                    )
            except ManualSftpError:
                raise
            except asyncssh.SFTPPermissionDenied as exc:
                raise self._terminal_failure(
                    record,
                    "SFTP_PERMISSION_DENIED",
                    "The server denied the remote removal.",
                ) from exc
            except Exception as exc:
                return await self._mutation_unknown(record, lease, exc)
            receipt = _terminal(
                operation_id, "succeeded", None, "The remote entry was removed."
            )
            self._put_terminal(record, "succeeded", receipt)
            return receipt
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED", "The server denied the remote removal."
            ) from exc
        finally:
            await lease.close()

    async def delete_preflight(
        self,
        ssh_session_id: UUID,
        path: str,
        *,
        operation_id: UUID,
    ) -> DeletePlanSummary:
        """Build a complete plan under the caller-selected durable identity."""

        root_path = validate_remote_path(path)
        self._require_new_operation(operation_id)
        lease = await self._channels.open(ssh_session_id)
        try:
            scan = await _scan_manifest(lease.client, root_path, root_path)
            if not scan.entries or scan.entries[0].manifest.entry_type != "directory":
                raise ManualSftpError(
                    "SFTP_NOT_A_DIRECTORY",
                    "Recursive delete requires a remote directory.",
                )
            delete_plan_id = uuid4()
            self._require_new_operation(operation_id)
            root_entry = next(
                item.manifest for item in scan.entries if item.manifest.path == root_path
            )
            root_snapshot = _manifest_snapshot(root_entry)
            summary = DeletePlanSummary(
                delete_plan_id=delete_plan_id,
                operation_id=operation_id,
                root_path=root_path,
                root_snapshot=root_snapshot,
                file_count=scan.file_count,
                directory_count=scan.directory_count,
                symlink_count=scan.symlink_count,
                total_byte_count=scan.total_byte_count,
                manifest_sha256=scan.sha256,
                complete=True,
            )
            tombstone_path = posixpath.join(
                posixpath.dirname(root_path) or "/",
                f".harness-shell-delete-{operation_id}.tombstone",
            )
            created_at = _utc_now()
            plan_record = DeletePlanRecord(
                delete_plan_id=delete_plan_id,
                operation_id=operation_id,
                connection_id=lease.owner.connection_id,
                connection_profile_version=lease.owner.connection_profile_version,
                host_label=lease.owner.host_label,
                target_host_key_fingerprint=lease.owner.target_host_key_fingerprint,
                jump_connection_id=lease.owner.jump_connection_id,
                jump_profile_version=lease.owner.jump_profile_version,
                jump_host_key_fingerprint=lease.owner.jump_host_key_fingerprint,
                root_path=root_path,
                tombstone_path=tombstone_path,
                summary=summary,
                manifest=tuple(item.manifest for item in scan.entries),
                created_at=created_at,
                consumed=False,
                terminal_receipt=None,
            )
            self._operations.put_delete_plan(plan_record)
            self._operations.put(
                RemoteOperationRecord(
                    operation_id=operation_id,
                    kind="recursive_delete",
                    state="preparing",
                    connection_id=lease.owner.connection_id,
                    connection_profile_version=lease.owner.connection_profile_version,
                    host_label=lease.owner.host_label,
                    target_host_key_fingerprint=lease.owner.target_host_key_fingerprint,
                    jump_connection_id=lease.owner.jump_connection_id,
                    jump_profile_version=lease.owner.jump_profile_version,
                    jump_host_key_fingerprint=lease.owner.jump_host_key_fingerprint,
                    remote_path=root_path,
                    temp_path=tombstone_path,
                    expected_sha256=scan.sha256,
                    target_snapshot=root_snapshot,
                    terminal_receipt=None,
                    created_at=created_at,
                )
            )
            self._delete_plans[delete_plan_id] = _DeletePlanState(
                ssh_session_id, plan_record
            )
            return summary
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the recursive-delete preflight.",
            ) from exc
        finally:
            await lease.close()

    async def delete_execute(
        self, delete_plan_id: UUID
    ) -> OperationTerminalProjection:
        """Consume one plan, isolate by atomic tombstone, rehash, then delete bottom-up."""

        state = self._delete_plans.get(delete_plan_id)
        persisted = self._operations.get_delete_plan(delete_plan_id)
        if persisted is None:
            raise ManualSftpError(
                "SFTP_DELETE_PLAN_NOT_FOUND", "The recursive-delete plan was not found."
            )
        if persisted.consumed:
            if persisted.terminal_receipt is not None:
                return persisted.terminal_receipt
            raise ManualSftpError(
                "SFTP_OPERATION_ALREADY_FINALIZED",
                "The recursive-delete plan is already finalized.",
            )
        if state is None:
            raise ManualSftpError(
                "SFTP_RECOVERY_REQUIRED",
                "The recursive-delete plan requires a new user-confirmed recovery action.",
            )
        record = state.record
        lease = await self._channels.open(state.ssh_session_id)
        tombstone_isolated = False
        try:
            current_root = await _snapshot(
                lease.client, record.root_path, include_hash=False
            )
            if current_root != record.summary.root_snapshot:
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED",
                    "The recursive-delete root changed after preflight.",
                )
            await self._emit(
                record, lease, "isolating", 0, len(record.manifest)
            )
            try:
                await _recursive_request(
                    lease.client.rename(
                        record.root_path.encode("utf-8"),
                        record.tombstone_path.encode("utf-8"),
                        flags=FXR_ATOMIC if lease.client.version >= 5 else 0,
                    )
                )
                tombstone_isolated = True
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise ManualSftpError(
                        "SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED",
                        "Cross-device tombstone isolation is not supported.",
                    ) from exc
                return await self._delete_unknown(record, lease, exc)
            except (
                asyncssh.SFTPFileAlreadyExists,
                asyncssh.SFTPNoSuchFile,
                asyncssh.SFTPNoSuchPath,
            ) as exc:
                error = map_typed_sftp_status(
                    exc,
                    exists_code="SFTP_TARGET_CHANGED",
                )
                assert error is not None
                raise error from exc
            except asyncssh.SFTPOpUnsupported as exc:
                raise ManualSftpError(
                    "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
                    "The server does not support atomic tombstone rename.",
                ) from exc
            except asyncssh.SFTPPermissionDenied as exc:
                raise ManualSftpError(
                    "SFTP_PERMISSION_DENIED",
                    "The server denied atomic tombstone isolation.",
                ) from exc
            except Exception as exc:
                return await self._delete_unknown(record, lease, exc)

            try:
                rescanned = await _scan_manifest(
                    lease.client, record.tombstone_path, record.root_path
                )
            except asyncssh.SFTPPermissionDenied as exc:
                raise self._delete_cleanup_failure(
                    record,
                    "SFTP_PERMISSION_DENIED",
                    "The server denied the isolated tombstone rescan.",
                ) from exc
            except Exception as exc:
                raise self._delete_cleanup_failure(
                    record,
                    "SFTP_TOMBSTONE_CLEANUP_REQUIRED",
                    "The isolated tombstone could not be rescanned.",
                ) from exc
            if rescanned.sha256 != record.summary.manifest_sha256:
                receipt = _terminal(
                    record.operation_id,
                    "cleanup_required",
                    "SFTP_TOMBSTONE_MANIFEST_CHANGED",
                    "The isolated directory changed and requires manual recovery.",
                    recovery_id=record.operation_id,
                )
                self._finalize_delete_plan(record, receipt, "cleanup_required")
                return receipt

            await self._emit(
                record, lease, "deleting", 0, len(rescanned.entries)
            )
            ordered = sorted(
                rescanned.entries,
                key=lambda item: (
                    item.actual_path.count("/"),
                    item.actual_path.encode("utf-8"),
                ),
                reverse=True,
            )
            completed = 0
            try:
                for item in ordered:
                    if item.manifest.entry_type == "directory":
                        await _recursive_request(
                            lease.client.rmdir(item.actual_path.encode("utf-8"))
                        )
                    else:
                        await _recursive_request(
                            lease.client.remove(item.actual_path.encode("utf-8"))
                        )
                    completed += 1
                    await self._emit(
                        record,
                        lease,
                        "deleting",
                        completed,
                        len(ordered),
                    )
            except asyncssh.SFTPPermissionDenied as exc:
                raise self._delete_cleanup_failure(
                    record,
                    "SFTP_PERMISSION_DENIED",
                    "The server denied tombstone cleanup.",
                ) from exc
            except Exception as exc:
                raise self._delete_cleanup_failure(
                    record,
                    "SFTP_TOMBSTONE_CLEANUP_REQUIRED",
                    "The isolated directory requires manual cleanup.",
                ) from exc
            if (await _snapshot(
                lease.client, record.tombstone_path, include_hash=False
            )).exists:
                receipt = _terminal(
                    record.operation_id,
                    "cleanup_required",
                    "SFTP_TOMBSTONE_CLEANUP_REQUIRED",
                    "The isolated directory still exists after deletion.",
                    recovery_id=record.operation_id,
                )
                self._finalize_delete_plan(record, receipt, "cleanup_required")
                return receipt
            receipt = _terminal(
                record.operation_id,
                "succeeded",
                None,
                "The remote directory was deleted from its isolated tombstone.",
            )
            self._finalize_delete_plan(record, receipt, "succeeded")
            return receipt
        except asyncssh.SFTPPermissionDenied as exc:
            state_name = "cleanup_required" if tombstone_isolated else "failed"
            recovery_id = record.operation_id if tombstone_isolated else None
            message = (
                "The server denied access to the isolated tombstone."
                if tombstone_isolated
                else "The server denied the recursive-delete verification."
            )
            receipt = _terminal(
                record.operation_id,
                state_name,
                "SFTP_PERMISSION_DENIED",
                message,
                recovery_id=recovery_id,
            )
            self._finalize_delete_plan(record, receipt, state_name)
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                message,
                operation_state=("cleanup_required" if tombstone_isolated else None),
            ) from exc
        except ManualSftpError as exc:
            persisted = self._operations.get(record.operation_id)
            if persisted is not None and persisted.state in {
                "preparing",
                "transferring",
                "verifying",
                "committing",
            }:
                receipt = _terminal(
                    record.operation_id,
                    "failed",
                    exc.error_code,
                    exc.safe_message,
                )
                self._finalize_delete_plan(record, receipt, "failed")
            raise
        finally:
            self._delete_plans.pop(delete_plan_id, None)
            await lease.close()

    async def close_all(self) -> None:
        """Discard only non-replayable live bindings; plaintext records remain."""

        self._delete_plans.clear()

    def _require_new_operation(self, operation_id: UUID) -> None:
        """Forbid reusing any persisted operation identity."""

        if self._operations.get(operation_id) is not None:
            raise ManualSftpError(
                "SFTP_OPERATION_DUPLICATE",
                "The manual SFTP operation ID cannot be reused.",
            )

    @staticmethod
    def _operation_record(
        operation_id: UUID,
        kind: str,
        lease: SftpChannelLease,
        remote_path: str,
        temp_path: str | None,
        snapshot: TransferSnapshot | None,
    ) -> RemoteOperationRecord:
        """Build one plaintext preparing record before dispatching a mutation."""

        return RemoteOperationRecord(
            operation_id=operation_id,
            kind=kind,
            state="preparing",
            connection_id=lease.owner.connection_id,
            connection_profile_version=lease.owner.connection_profile_version,
            host_label=lease.owner.host_label,
            target_host_key_fingerprint=lease.owner.target_host_key_fingerprint,
            jump_connection_id=lease.owner.jump_connection_id,
            jump_profile_version=lease.owner.jump_profile_version,
            jump_host_key_fingerprint=lease.owner.jump_host_key_fingerprint,
            remote_path=remote_path,
            temp_path=temp_path,
            expected_sha256=None,
            target_snapshot=snapshot,
            terminal_receipt=None,
            created_at=_utc_now(),
        )

    def _put_terminal(
        self,
        record: RemoteOperationRecord,
        state: str,
        receipt: OperationTerminalProjection,
    ) -> None:
        """Persist a trustworthy terminal receipt before returning it."""

        self._operations.put(
            record.model_copy(update={"state": state, "terminal_receipt": receipt})
        )

    def _terminal_failure(
        self,
        record: RemoteOperationRecord,
        error_code: str,
        message: str,
    ) -> ManualSftpError:
        """Persist one known non-mutating failure and return its public error."""

        receipt = _terminal(record.operation_id, "failed", error_code, message)
        self._put_terminal(record, "failed", receipt)
        return ManualSftpError(error_code, message)

    async def _mutation_unknown(
        self, record: RemoteOperationRecord, _lease: SftpChannelLease, exc: Exception
    ) -> OperationTerminalProjection:
        """Persist uncertainty after a mutation dispatch and never retry it."""

        receipt = _terminal(
            record.operation_id,
            "outcome_unknown",
            "SFTP_MUTATION_OUTCOME_UNKNOWN",
            "The remote mutation outcome could not be confirmed.",
            recovery_id=record.operation_id,
        )
        self._put_terminal(record, "outcome_unknown", receipt)
        raise ManualSftpError(
            "SFTP_MUTATION_OUTCOME_UNKNOWN",
            "The remote mutation outcome could not be confirmed.",
            operation_state="outcome_unknown",
        ) from exc

    async def _delete_unknown(
        self, record: DeletePlanRecord, _lease: SftpChannelLease, exc: Exception
    ) -> OperationTerminalProjection:
        """Persist uncertain tombstone rename without replaying the old operation."""

        receipt = _terminal(
            record.operation_id,
            "outcome_unknown",
            "SFTP_MUTATION_OUTCOME_UNKNOWN",
            "The tombstone isolation outcome could not be confirmed.",
            recovery_id=record.operation_id,
        )
        self._finalize_delete_plan(record, receipt, "outcome_unknown")
        raise ManualSftpError(
            "SFTP_MUTATION_OUTCOME_UNKNOWN",
            "The tombstone isolation outcome could not be confirmed.",
            operation_state="outcome_unknown",
        ) from exc

    def _delete_cleanup_failure(
        self,
        record: DeletePlanRecord,
        error_code: str,
        message: str,
    ) -> ManualSftpError:
        """Durably require recovery after a known or unknown cleanup failure."""

        receipt = _terminal(
            record.operation_id,
            "cleanup_required",
            error_code,
            message,
            recovery_id=record.operation_id,
        )
        self._finalize_delete_plan(record, receipt, "cleanup_required")
        return ManualSftpError(
            error_code,
            message,
            operation_state="cleanup_required",
        )

    def _finalize_delete_plan(
        self,
        record: DeletePlanRecord,
        receipt: OperationTerminalProjection,
        state: str,
    ) -> None:
        """Persist both operation receipt and consumed one-shot plan."""

        operation = self._operations.get(record.operation_id)
        if operation is None:
            raise ManualSftpError(
                "SFTP_OPERATION_RECORD_INVALID",
                "The recursive-delete operation record is missing.",
            )
        self._put_terminal(operation, state, receipt)
        self._operations.put_delete_plan(
            record.model_copy(update={"consumed": True, "terminal_receipt": receipt})
        )

    async def _emit(
        self,
        record: DeletePlanRecord,
        lease: SftpChannelLease,
        phase: str,
        completed: int,
        total: int,
    ) -> None:
        """Emit only the approved safe recursive-delete progress shape."""

        projection = MutationProgressProjection(
            operation_id=record.operation_id,
            kind="recursive_delete",
            phase=phase,
            display_name=posixpath.basename(record.root_path.rstrip("/")) or "/",
            remote_path=record.root_path,
            host_label=lease.owner.host_label,
            items_completed=completed,
            items_total=total,
            cancellable=False,
        )
        await self._event_listener(
            {
                "event": "manual_sftp.operation.progress",
                **projection.model_dump(mode="json"),
            }
        )


async def _scan_manifest(
    client: Any, actual_root: str, projected_root: str
) -> _ManifestScan:
    """Build a complete sorted UTF-8 JSONL manifest without following links."""

    scanned: list[_ScannedEntry] = []

    async def visit(actual_path: str, projected_path: str) -> None:
        if len(scanned) >= MAX_MANIFEST_ENTRIES:
            raise ManualSftpError(
                "SFTP_DIRECTORY_ENTRY_LIMIT_EXCEEDED",
                "The recursive manifest exceeds 50000 entries.",
            )
        try:
            async with asyncio.timeout(NO_PROGRESS_TIMEOUT_SECONDS):
                attrs = await client.lstat(actual_path.encode("utf-8"))
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_MANIFEST_TIMEOUT", "The recursive manifest made no progress."
            ) from exc
        entry = remote_entry(projected_path, attrs)
        link_target = None
        if entry.entry_type == "symlink":
            try:
                async with asyncio.timeout(NO_PROGRESS_TIMEOUT_SECONDS):
                    raw_target = await client.readlink(actual_path.encode("utf-8"))
            except TimeoutError as exc:
                raise ManualSftpError(
                    "SFTP_MANIFEST_TIMEOUT", "The recursive manifest made no progress."
                ) from exc
            link_target = _decode_name(raw_target)
        manifest = DeleteManifestEntry(
            path=projected_path,
            entry_type=entry.entry_type,
            size=entry.size,
            mode=entry.mode,
            mtime_ns=entry.mtime_ns,
            link_target=link_target,
        )
        scanned.append(_ScannedEntry(manifest, actual_path))
        if entry.entry_type != "directory":
            return
        iterator = client.scandir(actual_path.encode("utf-8"))
        try:
            while True:
                try:
                    async with asyncio.timeout(NO_PROGRESS_TIMEOUT_SECONDS):
                        child = await anext(iterator)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise ManualSftpError(
                        "SFTP_MANIFEST_TIMEOUT",
                        "The recursive manifest made no progress.",
                    ) from exc
                name = _decode_name(child.filename)
                if name in {".", ".."}:
                    continue
                await visit(
                    join_remote_path(actual_path, name),
                    join_remote_path(projected_path, name),
                )
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    await visit(validate_remote_path(actual_root), validate_remote_path(projected_root))
    ordered = tuple(
        sorted(scanned, key=lambda item: item.manifest.path.encode("utf-8"))
    )
    digest = hashlib.sha256()
    for item in ordered:
        encoded = json.dumps(
            item.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded + b"\n")
    return _ManifestScan(
        entries=ordered,
        sha256=digest.hexdigest(),
        file_count=sum(item.manifest.entry_type == "file" for item in ordered),
        directory_count=sum(
            item.manifest.entry_type == "directory" for item in ordered
        ),
        symlink_count=sum(
            item.manifest.entry_type == "symlink" for item in ordered
        ),
        total_byte_count=sum(
            item.manifest.size or 0
            for item in ordered
            if item.manifest.entry_type == "file"
        ),
    )


async def _filesystem_id(client: Any, path: str) -> int | None:
    """Read an OpenSSH statvfs filesystem ID when the server advertises it."""

    try:
        attributes = await _mutation_request(
            client.statvfs(validate_remote_path(path).encode("utf-8"))
        )
    except asyncssh.SFTPOpUnsupported:
        return None
    fsid = getattr(attributes, "fsid", None)
    if type(fsid) is not int or fsid < 0:
        raise ManualSftpError(
            "SFTP_REMOTE_RESPONSE_INVALID",
            "The SFTP server returned an invalid filesystem identifier.",
        )
    return fsid


async def _mutation_request(awaitable: Any) -> Any:
    """Apply the fixed single-request deadline without retrying a mutation."""

    try:
        async with asyncio.timeout(MUTATION_REQUEST_TIMEOUT_SECONDS):
            return await awaitable
    except TimeoutError as exc:
        raise ManualSftpError(
            "SFTP_OPERATION_TIMEOUT",
            "The remote SFTP mutation request timed out.",
        ) from exc


async def _recursive_request(awaitable: Any) -> Any:
    """Apply the recursive-delete no-progress window to one remote step."""

    try:
        async with asyncio.timeout(NO_PROGRESS_TIMEOUT_SECONDS):
            return await awaitable
    except TimeoutError as exc:
        raise ManualSftpError(
            "SFTP_MANIFEST_TIMEOUT",
            "The recursive-delete operation made no progress.",
        ) from exc


def _decode_name(value: Any) -> str:
    """Decode one filename/link target as strict UTF-8 without replacement."""

    try:
        if isinstance(value, bytes):
            return value.decode("utf-8", "strict")
        if isinstance(value, str):
            value.encode("utf-8", "strict")
            return value
    except UnicodeError as exc:
        raise ManualSftpError(
            "SFTP_FILENAME_ENCODING_UNSUPPORTED",
            "A remote filename is not valid UTF-8.",
        ) from exc
    raise ManualSftpError(
        "SFTP_REMOTE_RESPONSE_INVALID", "The SFTP server returned an invalid name."
    )


def _manifest_snapshot(entry: DeleteManifestEntry) -> TransferSnapshot:
    """Project one manifest entry into the canonical mutation snapshot shape."""

    return TransferSnapshot(
        path=entry.path,
        exists=True,
        entry_type=entry.entry_type,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        sha256=None,
    )


def _terminal(
    operation_id: UUID,
    state: str,
    error_code: str | None,
    message: str,
    *,
    recovery_id: UUID | None = None,
) -> OperationTerminalProjection:
    """Build a strict safe mutation terminal receipt."""

    return OperationTerminalProjection(
        operation_id=operation_id,
        state=state,
        error_code=error_code,
        message=message,
        sha256=None,
        byte_count=None,
        recovery_id=recovery_id,
    )


def _utc_now() -> str:
    """Return a stable RFC 3339 UTC timestamp for plaintext records."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
