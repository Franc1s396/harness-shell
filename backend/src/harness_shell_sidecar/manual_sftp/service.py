"""用户手动 SFTP 浏览、传输、变更和恢复服务。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import asyncssh

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import PlaintextRecordStore

from .channels import SftpChannelFactory, SftpChannelLease
from .errors import ManualSftpError, map_typed_sftp_status
from .listings import ListingManager, remote_entry
from .models import (
    DeletePlanSummary,
    DownloadChunk,
    DownloadReady,
    ListingBatch,
    ManualSftpContext,
    OperationTerminalProjection,
    RecoverySummary,
    RemoteEntry,
    RemoteFileHash,
    TransferSnapshot,
    UploadChunkAck,
    UploadReady,
)
from .mutations import MutationManager
from .operation_store import ManualSftpOperationStore
from .paths import validate_remote_path
from .recovery import RecoveryManager
from .transfers import DownloadManager, UploadManager


METADATA_TIMEOUT_SECONDS = 15
HASH_NO_PROGRESS_TIMEOUT_SECONDS = 60
HASH_CHUNK_BYTES = 64 * 1024
MAX_JS_SAFE_INTEGER = 2**53 - 1


class ManualSftpService:
    """拥有全部手动 SFTP 通道、游标、操作和恢复状态。"""

    def __init__(
        self,
        ssh_sessions: SshSessionRegistry,
        records: PlaintextRecordStore,
        event_listener: Callable[[dict], Awaitable[None]],
    ) -> None:
        """创建读取资源管理者，并保留后续阶段需要的协作者。"""

        self._channels = SftpChannelFactory(ssh_sessions)
        self._listings = ListingManager(self._channels)
        self._records = records
        self._event_listener = event_listener
        self._operations = ManualSftpOperationStore(records)
        self._uploads = UploadManager(self._channels, self._operations)
        self._downloads = DownloadManager(self._channels)
        self._mutations = MutationManager(
            self._channels, self._operations, self._event_listener
        )
        self._recovery = RecoveryManager(
            self._channels, self._operations, self._mutations
        )

    async def open(self, ssh_session_id: UUID) -> ManualSftpContext:
        """在显式绑定的活动 SSH 会话上解析主目录。"""

        lease = await self._channels.open(ssh_session_id)
        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                home = _decode_remote_path(await lease.client.getcwd())
            return ManualSftpContext(
                ssh_session_id=ssh_session_id,
                connection_id=lease.owner.connection_id,
                home=validate_remote_path(home),
                host_label=lease.owner.host_label,
                sftp_version=lease.client.version,
            )
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied access to the remote home directory.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        finally:
            await lease.close()

    async def list_begin(self, ssh_session_id: UUID, path: str) -> ListingBatch:
        """开始有界目录列表。"""

        return await self._listings.begin(ssh_session_id, path)

    async def list_next(self, listing_id: UUID, sequence: int) -> ListingBatch:
        """按精确下一序号继续活动目录列表。"""

        return await self._listings.next(listing_id, sequence)

    async def list_close(self, listing_id: UUID) -> None:
        """显式关闭列表游标。"""

        await self._listings.close(listing_id)

    async def lstat(self, ssh_session_id: UUID, path: str) -> RemoteEntry:
        """读取元数据，不跟随符号链接。"""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                attrs = await lease.client.lstat(remote_path.encode("utf-8"))
            return remote_entry(remote_path, attrs)
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote metadata request.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        finally:
            await lease.close()

    async def readlink(self, ssh_session_id: UUID, path: str) -> RemoteEntry:
        """返回显式链接文本，同时保留不跟随链接的元数据。"""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                encoded = remote_path.encode("utf-8")
                attrs = await lease.client.lstat(encoded)
                target = _decode_remote_text(await lease.client.readlink(encoded))
            entry = remote_entry(remote_path, attrs, link_target=target)
            if entry.entry_type != "symlink":
                raise ManualSftpError(
                    "SFTP_NOT_A_SYMLINK", "The selected remote entry is not a symlink."
                )
            return entry
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote link request.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        finally:
            await lease.close()

    async def sha256(
        self,
        ssh_session_id: UUID,
        path: str,
        *,
        cancelled: asyncio.Event | None = None,
    ) -> RemoteFileHash:
        """计算普通文件哈希；返回前发现元数据变化则拒绝。"""

        # 1. 校验路径并冻结普通文件快照，拒绝目录或链接作为哈希源。
        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            _require_active(cancelled)
            initial = await self._snapshot(lease, remote_path)
            if not initial.exists or initial.entry_type != "file":
                raise ManualSftpError(
                    "SFTP_NOT_A_REGULAR_FILE",
                    "The selected remote entry is not a regular file.",
                )
            # 2. 分块读取并累计哈希，每次读取检查取消和无进展超时。
            digest = hashlib.sha256()
            byte_count = 0
            async with await lease.client.open(remote_path.encode("utf-8"), "rb") as file:
                while True:
                    _require_active(cancelled)
                    try:
                        async with asyncio.timeout(HASH_NO_PROGRESS_TIMEOUT_SECONDS):
                            chunk = await file.read(HASH_CHUNK_BYTES)
                    except TimeoutError as exc:
                        raise ManualSftpError(
                            "SFTP_HASH_TIMEOUT", "The remote file hash made no progress."
                        ) from exc
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes) or len(chunk) > HASH_CHUNK_BYTES:
                        raise ManualSftpError(
                            "SFTP_REMOTE_READ_INVALID",
                            "The remote file returned an invalid read chunk.",
                        )
                    byte_count += len(chunk)
                    if byte_count > MAX_JS_SAFE_INTEGER:
                        raise ManualSftpError(
                            "SFTP_FILE_SIZE_UNSUPPORTED",
                            "The remote file size is outside the supported range.",
                        )
                    digest.update(chunk)
            _require_active(cancelled)
            # 3. 完整读取后复核元数据和字节数，只有源未变才返回哈希。
            final = await self._snapshot(lease, remote_path)
            if final != initial or (
                initial.size is not None and byte_count != initial.size
            ):
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED", "The remote file changed during hashing."
                )
            sha256 = digest.hexdigest()
            return RemoteFileHash(
                path=remote_path,
                snapshot=initial.model_copy(update={"sha256": sha256}),
                sha256=sha256,
                byte_count=byte_count,
            )
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote file hash request.",
            ) from exc
        finally:
            await lease.close()

    async def upload_preflight(
        self, ssh_session_id: UUID, path: str
    ) -> TransferSnapshot:
        """在用户确认覆盖前冻结远程上传目标。"""

        return await self._uploads.preflight(ssh_session_id, path)

    async def upload_begin(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        path: str,
        source_sha256: str,
        source_byte_count: int,
        target_snapshot: TransferSnapshot,
    ) -> UploadReady:
        """根据 React 提供的冻结源文件元数据开始上传。"""

        return await self._uploads.begin(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            path=path,
            source_sha256=source_sha256,
            source_byte_count=source_byte_count,
            target_snapshot=target_snapshot,
        )

    async def upload_chunk(
        self,
        operation_id: UUID,
        *,
        sequence: int,
        offset: int,
        chunk: bytes,
    ) -> UploadChunkAck:
        """写入一个精确上传分块。"""

        return await self._uploads.write_chunk(
            operation_id=operation_id,
            sequence=sequence,
            offset=offset,
            chunk=chunk,
        )

    async def upload_finish(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """验证并原子提交上传。"""

        return await self._uploads.finish(operation_id)

    async def upload_abort(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """取消活动上传并删除远程临时文件。"""

        return await self._uploads.abort(operation_id)

    async def download_begin(
        self, *, operation_id: UUID, ssh_session_id: UUID, path: str
    ) -> DownloadReady:
        """冻结并打开远程下载源。"""

        return await self._downloads.begin(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            path=path,
        )

    async def download_chunk(
        self, operation_id: UUID, *, sequence: int, offset: int
    ) -> DownloadChunk:
        """拉取一个顺序远程下载分块。"""

        return await self._downloads.read_chunk(
            operation_id, sequence=sequence, offset=offset
        )

    async def download_finish(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """验证已完整拉取的远程源文件。"""

        return await self._downloads.finish(operation_id)

    async def download_abort(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """取消活动远程下载读取。"""

        return await self._downloads.abort(operation_id)

    async def mkdir(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        parent_path: str,
        name: str,
    ) -> OperationTerminalProjection:
        """创建用户请求的远程目录。"""

        return await self._mutations.mkdir(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            parent_path=parent_path,
            name=name,
        )

    async def rename(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        source_path: str,
        target_path: str,
        overwrite: bool,
        source_snapshot: TransferSnapshot | None,
        target_snapshot: TransferSnapshot | None,
    ) -> OperationTerminalProjection:
        """原子重命名未改变的远程条目。"""

        return await self._mutations.rename(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            source_path=source_path,
            target_path=target_path,
            overwrite=overwrite,
            source_snapshot=source_snapshot,
            target_snapshot=target_snapshot,
        )

    async def remove(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        path: str,
        expected_snapshot: TransferSnapshot,
    ) -> OperationTerminalProjection:
        """删除未改变的文件、链接或空目录。"""

        return await self._mutations.remove(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            path=path,
            expected_snapshot=expected_snapshot,
        )

    async def delete_preflight(
        self, operation_id: UUID, ssh_session_id: UUID, path: str
    ) -> DeletePlanSummary:
        """构建完整明文、不跟随链接的递归删除计划。"""

        return await self._mutations.delete_preflight(
            ssh_session_id, path, operation_id=operation_id
        )

    async def delete_execute(
        self, delete_plan_id: UUID
    ) -> OperationTerminalProjection:
        """最多消费一次递归删除计划。"""

        return await self._mutations.delete_execute(delete_plan_id)

    def list_recoveries(self) -> tuple[RecoverySummary, ...]:
        """返回仅含远程状态的非终态恢复摘要。"""

        return self._recovery.list()

    async def recovery_inspect(self, recovery_id: UUID):
        """对恢复记录执行只读核对。"""

        return await self._recovery.inspect(recovery_id)

    async def recovery_execute(
        self, recovery_id: UUID, action: str, operation_id: UUID
    ):
        """使用 React 选择的新操作标识执行恢复操作。"""

        return await self._recovery.execute(recovery_id, action, operation_id)

    async def close_all(self) -> None:
        """先关闭列表和传输管理者，再关闭父级 SSH 注册表。"""

        first_error: BaseException | None = None
        for owner in (
            self._listings,
            self._uploads,
            self._downloads,
            self._mutations,
            self._recovery,
        ):
            try:
                await owner.close_all()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    async def _snapshot(
        lease: SftpChannelLease, remote_path: str
    ) -> TransferSnapshot:
        """捕获严格的不跟随链接元数据，用于返回前比较。"""

        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                attrs = await lease.client.lstat(remote_path.encode("utf-8"))
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        entry = remote_entry(remote_path, attrs)
        return TransferSnapshot(
            path=remote_path,
            exists=True,
            entry_type=entry.entry_type,
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            sha256=None,
        )


def _decode_remote_text(value: Any) -> str:
    """严格按 UTF-8 解码 AsyncSSH 公共接口的字节结果。"""

    try:
        if isinstance(value, bytes):
            return value.decode("utf-8", "strict")
        if isinstance(value, str):
            value.encode("utf-8", "strict")
            return value
    except UnicodeError as exc:
        raise ManualSftpError(
            "SFTP_FILENAME_ENCODING_UNSUPPORTED", "A remote path is not valid UTF-8."
        ) from exc
    raise ManualSftpError(
        "SFTP_REMOTE_RESPONSE_INVALID", "The SFTP server returned an invalid path."
    )


def _decode_remote_path(value: Any) -> str:
    """解码并校验远程绝对路径响应。"""

    return validate_remote_path(_decode_remote_text(value))


def _require_active(cancelled: asyncio.Event | None) -> None:
    """取消后在定义好的边界停止协作式工作。"""

    if cancelled is not None and cancelled.is_set():
        raise ManualSftpError(
            "SFTP_REQUEST_CANCELLED", "The manual SFTP request was cancelled."
        )
