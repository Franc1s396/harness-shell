"""不跟随链接的手动 SFTP 变更、墓碑目录与递归删除计划。"""

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
    """将规范投影清单元数据与实际远程路径配对。"""

    #: 用于哈希和明文持久化的规范条目。
    manifest: DeleteManifestEntry
    #: 仅由当前内存操作使用的实际路径。
    actual_path: str


@dataclass(frozen=True, slots=True)
class _ManifestScan:
    """保存完整的不跟随链接扫描结果及确定性摘要和计数。"""

    #: 按规范 UTF-8 路径字节排序的条目。
    entries: tuple[_ScannedEntry, ...]
    #: 排序后的紧凑 JSON Lines 记录的 SHA-256。
    sha256: str
    #: 普通文件数量。
    file_count: int
    #: 包含根目录的目录数量。
    directory_count: int
    #: 不计算链接目标的符号链接数量。
    symlink_count: int
    #: 已知普通文件大小之和。
    total_byte_count: int


@dataclass(slots=True)
class _DeletePlanState:
    """保留执行计划所需的非持久化活动 SSH 绑定。"""

    #: 预检时选择的活动 SSH 会话，不持久化。
    ssh_session_id: UUID
    #: 不含活动会话 ID 的明文规范计划记录。
    record: DeletePlanRecord


class MutationManager:
    """负责严格单次派发变更和一次性递归删除计划。"""

    def __init__(
        self,
        channels: SftpChannelFactory,
        operations: ManualSftpOperationStore,
        event_listener: Callable[[dict], Awaitable[None]],
    ) -> None:
        """绑定通道、明文状态和安全进展投影管理者。"""

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
        """对不存在的子目录仅执行一次创建。"""

        # 1. 校验单个目录名和新操作标识，打开所选会话的独立通道。
        target_path = join_remote_path(parent_path, validate_basename(name))
        self._require_new_operation(operation_id)
        lease = await self._channels.open(ssh_session_id)
        try:
            if (await _snapshot(lease.client, target_path, include_hash=False)).exists:
                raise ManualSftpError(
                    "SFTP_TARGET_EXISTS", "The remote directory already exists."
                )
            # 目标已存在可证明尚未尝试变更，因此只有确定性的前置条件
            # 检查成功后才持久化操作意图。
            # 2. 已确认目标不存在后持久化意图，再执行唯一一次 mkdir。
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
            # 3. 已确认成功后持久化回执；派发后不确定失败进入恢复状态。
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
        """复核两个名称并执行一次原子重命名，不降级。"""

        # 1. 校验路径和新操作标识，并读取两端当前快照。
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
            # 2. 快照匹配后检查文件系统身份，拒绝已知跨文件系统重命名。
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
            # 3. 持久化操作意图，选择协议支持的原子标志后只派发一次。
            record = self._operation_record(
                operation_id,
                "rename",
                lease,
                target,
                source,
                observed_target,
            )
            self._operations.put(record)
            # SFTP v3 中任意非零标志都会选择 OpenSSH posix-rename，可能覆盖
            # 在目标不存在快照之后新建的条目；此时应使用标准的不覆盖重命名。
            # SFTP v5 及以上版本才有显式的原子且不覆盖标志。
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
            # 4. 确定成功后持久化终态，派发后的不确定失败交给恢复流程。
            # 4. 确定成功才写成功回执；不确定结果保留恢复记录，不重试。
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
        """删除未改变的文件或链接，或已证实为空的目录。"""

        # 1. 校验目标路径和新操作标识，取得独立通道。
        remote_path = validate_remote_path(path)
        self._require_new_operation(operation_id)
        lease = await self._channels.open(ssh_session_id)
        try:
            # 2. 复核完整快照；目录还需证明为空，避免记录尚未可执行的意图。
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
            # 目录为空是派发前的确定性检查；检查通过后，
            # 紧邻唯一一次 remove/rmdir 调用之前才持久化意图。
            # 3. 前置检查通过后持久化意图，再按类型仅删除一次。
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
        """按调用方选择的持久化标识构建完整计划。"""

        # 1. 校验根路径和全新操作标识，使用独立通道执行只读预检。
        root_path = validate_remote_path(path)
        self._require_new_operation(operation_id)
        lease = await self._channels.open(ssh_session_id)
        try:
            # 2. 完整扫描且不跟随链接，冻结清单哈希、数量和根快照。
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
            # 3. 确定同目录墓碑路径，并持久化完整一次性计划和准备记录。
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
            # 4. 只在内存保存执行所需活动会话绑定，然后返回供用户确认的摘要。
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
        """消费计划，原子重命名为墓碑目录隔离，重新校验哈希后自底向上删除。"""

        # 1. 同时检查持久化消费状态和本次进程的活动绑定，禁止重放旧计划。
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
            # 2. 复核根快照，再以原子重命名隔离到墓碑路径。
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
            # 3. 隔离后重新扫描并比较完整清单哈希，变化时停止并要求恢复。
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
            # 4. 按深度自底向上删除；逐项操作受无进展超时约束。
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
            # 5. 确认墓碑路径已消失后，持久化成功回执并消费一次性计划。
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
        """仅丢弃不可回放的活动绑定；保留明文记录。"""

        self._delete_plans.clear()

    def _require_new_operation(self, operation_id: UUID) -> None:
        """禁止复用任何已持久化的操作标识。"""

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
        """派发变更前构建明文准备状态记录。"""

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
        """返回可信终态回执前先持久化。"""

        self._operations.put(
            record.model_copy(update={"state": state, "terminal_receipt": receipt})
        )

    def _terminal_failure(
        self,
        record: RemoteOperationRecord,
        error_code: str,
        message: str,
    ) -> ManualSftpError:
        """持久化已知未发生变更的失败并返回公开错误。"""

        receipt = _terminal(record.operation_id, "failed", error_code, message)
        self._put_terminal(record, "failed", receipt)
        return ManualSftpError(error_code, message)

    async def _mutation_unknown(
        self, record: RemoteOperationRecord, _lease: SftpChannelLease, exc: Exception
    ) -> OperationTerminalProjection:
        """变更派发后持久化不确定状态，绝不重试该变更。"""

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
        """持久化墓碑重命名的不确定状态，不回放旧操作。"""

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
        """已知或未知清理失败后，持久化需要恢复的状态。"""

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
        """同时持久化操作回执和已消费的一次性计划。"""

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
        """仅发出批准的安全递归删除进展结构。"""

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
    """构建完整有序的 UTF-8 JSONL 清单，不跟随链接。"""

    # 1. 收集实际路径与规范投影条目，递归过程始终不跟随符号链接。
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

    # 2. 完整遍历后按 UTF-8 路径字节排序，消除服务器枚举顺序差异。
    await visit(validate_remote_path(actual_root), validate_remote_path(projected_root))
    ordered = tuple(
        sorted(scanned, key=lambda item: item.manifest.path.encode("utf-8"))
    )
    # 3. 对规范 JSONL 计算哈希并汇总数量，供执行前后比较。
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
    """服务器声明支持时读取 OpenSSH statvfs 文件系统 ID。"""

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
    """应用固定单请求截止时间，不重试变更。"""

    try:
        async with asyncio.timeout(MUTATION_REQUEST_TIMEOUT_SECONDS):
            return await awaitable
    except TimeoutError as exc:
        raise ManualSftpError(
            "SFTP_OPERATION_TIMEOUT",
            "The remote SFTP mutation request timed out.",
        ) from exc


async def _recursive_request(awaitable: Any) -> Any:
    """将递归删除的无进展超时窗口应用到远程步骤。"""

    try:
        async with asyncio.timeout(NO_PROGRESS_TIMEOUT_SECONDS):
            return await awaitable
    except TimeoutError as exc:
        raise ManualSftpError(
            "SFTP_MANIFEST_TIMEOUT",
            "The recursive-delete operation made no progress.",
        ) from exc


def _decode_name(value: Any) -> str:
    """严格按 UTF-8 解码文件名或链接目标，不替换字符。"""

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
    """将清单条目投影为规范变更快照结构。"""

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
    """构建严格安全的变更终态回执。"""

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
    """为明文记录返回稳定的 RFC 3339 UTC 时间戳。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
