"""手动 SFTP 的严格上传和下载状态机。"""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncssh
from asyncssh.constants import FXR_ATOMIC, FXR_OVERWRITE

from .channels import SftpChannelFactory, SftpChannelLease
from .errors import ManualSftpError, map_typed_sftp_status
from .listings import remote_entry
from .models import (
    DownloadChunk,
    DownloadReady,
    OperationTerminalProjection,
    TransferSnapshot,
    UploadChunkAck,
    UploadReady,
)
from .operation_store import ManualSftpOperationStore, RemoteOperationRecord
from .paths import validate_remote_path


SFTP_CHUNK_BYTES = 262_144
HASH_CHUNK_BYTES = 64 * 1024
NO_PROGRESS_TIMEOUT_SECONDS = 60
CHUNK_REQUEST_TIMEOUT_SECONDS = 30
METADATA_TIMEOUT_SECONDS = 15
MAX_JS_SAFE_INTEGER = 2**53 - 1


@dataclass(slots=True)
class _UploadState:
    """拥有上传通道、句柄、序号、偏移量及规范快照。"""

    #: 与 React 和明文记录共享的稳定操作标识。
    operation_id: UUID
    #: 独立 SFTP 通道租约。
    lease: SftpChannelLease
    #: 独占远程临时文件句柄；关闭后为 None。
    handle: Any | None
    #: 已校验的最终远程目标路径。
    target_path: str
    #: 同目录下的独占临时路径。
    temp_path: str
    #: 预检及开始阶段复核得到的冻结目标快照。
    target_snapshot: TransferSnapshot
    #: React 提供的冻结本地源文件字节数。
    source_byte_count: int
    #: React 提供的冻结本地源文件哈希。
    source_sha256: str
    #: 下一分块必须使用的序号。
    expected_sequence: int
    #: 下一分块必须使用的偏移量。
    expected_offset: int
    #: 已接收上传字节的流式摘要。
    digest: Any
    #: 状态更新间复用的不可变记录创建时间戳。
    created_at: str


@dataclass(slots=True)
class _DownloadState:
    """拥有下载通道、句柄、位置及冻结的远程源。"""

    #: 稳定操作标识。
    operation_id: UUID
    #: 独立 SFTP 通道租约。
    lease: SftpChannelLease
    #: 已打开的远程读取句柄；关闭后为 None。
    handle: Any | None
    #: 已校验的源路径。
    source_path: str
    #: 冻结元数据及必需的源文件哈希。
    source_snapshot: TransferSnapshot
    #: 下一拉取请求必须使用的序号。
    expected_sequence: int
    #: 下一拉取请求必须使用的偏移量。
    expected_offset: int
    #: 返回给 React 的字节的流式摘要。
    digest: Any
    #: 已返回分块是否证明当前状态到达 EOF。
    eof: bool


class UploadManager:
    """管理远程上传临时文件，直到严格原子提交或清理完成。"""

    def __init__(
        self, channels: SftpChannelFactory, operations: ManualSftpOperationStore
    ) -> None:
        """绑定通道和明文操作管理者。"""

        self._channels = channels
        self._operations = operations
        self._active: dict[UUID, _UploadState] = {}

    async def preflight(
        self, ssh_session_id: UUID, path: str
    ) -> TransferSnapshot:
        """冻结当前目标；普通文件必须包含哈希。"""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            return await _snapshot(lease.client, remote_path, include_hash=True)
        finally:
            await lease.close()

    async def begin(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        path: str,
        source_sha256: str,
        source_byte_count: int,
        target_snapshot: TransferSnapshot,
    ) -> UploadReady:
        """持久化意图、复核目标，再独占创建临时文件。"""

        # 1. 校验本地元数据、目标路径和操作标识，拒绝重复或已终结操作。
        self._validate_source(source_sha256, source_byte_count)
        target_path = validate_remote_path(path)
        if target_snapshot.path != target_path:
            raise ManualSftpError(
                "SFTP_TARGET_SNAPSHOT_INVALID",
                "The upload target snapshot does not match the target path.",
            )
        existing = self._operations.get(operation_id)
        if operation_id in self._active or existing is not None:
            error_code = (
                "SFTP_OPERATION_ALREADY_FINALIZED"
                if existing is not None
                and existing.state in {"succeeded", "failed", "cancelled"}
                else "SFTP_OPERATION_DUPLICATE"
            )
            raise ManualSftpError(
                error_code, "The manual SFTP operation ID cannot be reused."
            )

        # 2. 建立独立通道并持久化准备记录，保留后续恢复所需的远程身份。
        lease = await self._channels.open(ssh_session_id)
        temp_path = _upload_temp_path(target_path, operation_id)
        created_at = _utc_now()
        preparing = RemoteOperationRecord(
            operation_id=operation_id,
            kind="upload",
            state="preparing",
            connection_id=lease.owner.connection_id,
            connection_profile_version=lease.owner.connection_profile_version,
            host_label=lease.owner.host_label,
            target_host_key_fingerprint=lease.owner.target_host_key_fingerprint,
            jump_connection_id=lease.owner.jump_connection_id,
            jump_profile_version=lease.owner.jump_profile_version,
            jump_host_key_fingerprint=lease.owner.jump_host_key_fingerprint,
            remote_path=target_path,
            temp_path=temp_path,
            expected_sha256=source_sha256,
            target_snapshot=target_snapshot,
            terminal_receipt=None,
            created_at=created_at,
        )
        self._operations.put(preparing)
        try:
            # 3. 复核目标快照后独占创建临时文件，前置失败必须清理通道。
            current = await _snapshot(
                lease.client, target_path, include_hash=target_snapshot.exists
            )
            if current != target_snapshot:
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED",
                    "The remote upload target changed after preflight.",
                )
            try:
                handle = await _metadata_await(
                    lease.client.open(temp_path.encode("utf-8"), "xb")
                )
            except asyncssh.SFTPFileAlreadyExists as exc:
                raise ManualSftpError(
                    "SFTP_TEMP_PATH_CONFLICT",
                    "The exclusive remote upload temporary path already exists.",
                ) from exc
        except BaseException as exc:
            receipt = _terminal(
                operation_id,
                "failed",
                exc.error_code if isinstance(exc, ManualSftpError) else "SFTP_UPLOAD_BEGIN_FAILED",
                "The remote upload could not be started.",
            )
            self._operations.put(
                preparing.model_copy(
                    update={"state": "failed", "terminal_receipt": receipt}
                )
            )
            await lease.close()
            raise

        # 4. 将句柄交给上传状态管理，登记活动操作并发布 transferring。
        state = _UploadState(
            operation_id=operation_id,
            lease=lease,
            handle=handle,
            target_path=target_path,
            temp_path=temp_path,
            target_snapshot=target_snapshot,
            source_byte_count=source_byte_count,
            source_sha256=source_sha256,
            expected_sequence=0,
            expected_offset=0,
            digest=hashlib.sha256(),
            created_at=created_at,
        )
        self._active[operation_id] = state
        self._put_upload_state(state, "transferring")
        return UploadReady(
            operation_id=operation_id,
            temp_path=temp_path,
            next_sequence=0,
            next_offset=0,
        )

    async def write_chunk(
        self,
        *,
        operation_id: UUID,
        sequence: int,
        offset: int,
        chunk: bytes,
    ) -> UploadChunkAck:
        """仅接收一个非空、有界、顺序正确的分块。"""

        # 1. 只接受活动操作的精确序号和偏移量，同时限制分块与总字节数。
        state = self._require_upload(operation_id)
        if sequence != state.expected_sequence or offset != state.expected_offset:
            error = ManualSftpError(
                "SFTP_PROTOCOL_SEQUENCE_INVALID",
                "The upload sequence or offset is invalid.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error
        if not isinstance(chunk, bytes) or not 1 <= len(chunk) <= SFTP_CHUNK_BYTES:
            error = ManualSftpError(
                "SFTP_CHUNK_INVALID", "Upload chunks must contain 1..262144 bytes."
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error
        if state.expected_offset + len(chunk) > state.source_byte_count:
            error = ManualSftpError(
                "SFTP_SOURCE_SIZE_MISMATCH",
                "The upload bytes exceed the frozen source size.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error
        try:
            # 2. 在单分块截止时间内写入；失败时进入统一上传清理路径。
            async with asyncio.timeout(CHUNK_REQUEST_TIMEOUT_SECONDS):
                await state.handle.write(chunk)
        except TimeoutError as exc:
            error = ManualSftpError(
                "SFTP_TRANSFER_TIMEOUT", "The upload chunk made no progress."
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        except Exception as exc:
            error = ManualSftpError(
                "SFTP_UPLOAD_WRITE_FAILED", "The remote upload chunk failed."
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        # 3. 写入成功后才推进哈希、序号和偏移，并返回对应确认。
        state.digest.update(chunk)
        state.expected_sequence += 1
        state.expected_offset += len(chunk)
        return UploadChunkAck(
            operation_id=operation_id,
            sequence=sequence,
            offset=offset,
            accepted_bytes=len(chunk),
        )

    async def finish(self, operation_id: UUID) -> OperationTerminalProjection:
        """验证完整字节、临时文件和目标，再执行一次带标志的原子重命名。"""

        # 1. 确认已收齐声明字节且流式哈希匹配，缺失或不匹配时中止。
        state = self._require_upload(operation_id)
        if (
            state.expected_offset != state.source_byte_count
            or state.digest.hexdigest() != state.source_sha256
        ):
            error = ManualSftpError(
                "SFTP_SOURCE_HASH_MISMATCH",
                "The uploaded bytes do not match the frozen local source.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error

        # 2. 先关闭写句柄，再校验临时文件完整快照和哈希。
        self._put_upload_state(state, "verifying")
        await _close_handle(state)
        try:
            temp_snapshot = await _snapshot(
                state.lease.client, state.temp_path, include_hash=True
            )
        except ManualSftpError as exc:
            await self._fail_upload(state, exc, remove_temp=True)
            raise
        if (
            temp_snapshot.entry_type != "file"
            or temp_snapshot.size != state.source_byte_count
            or temp_snapshot.sha256 != state.source_sha256
        ):
            error = ManualSftpError(
                "SFTP_REMOTE_TEMP_VERIFICATION_FAILED",
                "The remote upload temporary file failed verification.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error

        try:
            # 3. 提交前复核最终目标仍与预检快照一致，防止覆盖并发修改。
            current_target = await _snapshot(
                state.lease.client,
                state.target_path,
                include_hash=state.target_snapshot.exists,
            )
        except ManualSftpError as exc:
            await self._fail_upload(state, exc, remove_temp=True)
            raise
        if current_target != state.target_snapshot:
            error = ManualSftpError(
                "SFTP_TARGET_CHANGED",
                "The remote upload target changed before commit.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error

        # 4. 先持久化 committing，再执行一次协议允许的原子重命名。
        self._put_upload_state(state, "committing")
        # AsyncSSH 将所有非零 v3 标志映射为 OpenSSH posix-rename；
        # 冻结目标原本不存在时，其覆盖语义不安全。标准 v3 rename
        # 是协议的不覆盖形式；v5 及以上版本可直接表达原子且不覆盖。
        flags = (
            FXR_ATOMIC | FXR_OVERWRITE
            if state.target_snapshot.exists
            else (FXR_ATOMIC if state.lease.client.version >= 5 else 0)
        )
        try:
            await _metadata_await(
                state.lease.client.rename(
                    state.temp_path.encode("utf-8"),
                    state.target_path.encode("utf-8"),
                    flags=flags,
                )
            )
        except asyncssh.SFTPOpUnsupported as exc:
            error = ManualSftpError(
                "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
                "The server does not support the required atomic rename.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        except (
            asyncssh.SFTPFileAlreadyExists,
            asyncssh.SFTPNoSuchFile,
            asyncssh.SFTPNoSuchPath,
            asyncssh.SFTPPermissionDenied,
        ) as exc:
            error = map_typed_sftp_status(
                exc,
                exists_code="SFTP_TARGET_CHANGED",
            )
            assert error is not None
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        except (asyncssh.SFTPError, ManualSftpError) as exc:
            self._active.pop(operation_id, None)
            receipt = _terminal(
                operation_id,
                "outcome_unknown",
                "SFTP_COMMIT_OUTCOME_UNKNOWN",
                "The remote atomic commit outcome could not be confirmed.",
                recovery_id=operation_id,
            )
            self._put_upload_state(state, "outcome_unknown", receipt)
            await state.lease.close()
            raise ManualSftpError(
                "SFTP_COMMIT_OUTCOME_UNKNOWN",
                "The remote atomic commit outcome could not be confirmed.",
                operation_state="outcome_unknown",
            ) from exc

        # 5. 只有确认提交成功才移除活动状态、保存成功回执并关闭通道。
        self._active.pop(operation_id, None)
        receipt = _terminal(
            operation_id,
            "succeeded",
            None,
            "The remote upload completed atomically.",
            sha256=state.source_sha256,
            byte_count=state.source_byte_count,
        )
        self._put_upload_state(state, "succeeded", receipt)
        await state.lease.close()
        return receipt

    async def abort(self, operation_id: UUID) -> OperationTerminalProjection:
        """关闭并删除活动临时上传，不提交。"""

        state = self._require_upload(operation_id)
        receipt = _terminal(
            operation_id, "cancelled", None, "The remote upload was cancelled."
        )
        await self._close_upload(state, remove_temp=True, receipt=receipt)
        return receipt

    async def close_all(self) -> None:
        """中止全部活动上传，并保留首个清理失败。"""

        first_error: BaseException | None = None
        for operation_id in tuple(self._active):
            try:
                await self.abort(operation_id)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _validate_source(source_sha256: str, source_byte_count: int) -> None:
        """打开远程通道前拒绝不可信直接调用方。"""

        if (
            len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
            or type(source_byte_count) is not int
            or not 0 <= source_byte_count <= MAX_JS_SAFE_INTEGER
        ):
            raise ManualSftpError(
                "SFTP_SOURCE_SNAPSHOT_INVALID",
                "The frozen upload source snapshot is invalid.",
            )

    def _require_upload(self, operation_id: UUID) -> _UploadState:
        """返回活动上传，或区分已终结与未知 ID。"""

        state = self._active.get(operation_id)
        if state is not None:
            return state
        record = self._operations.get(operation_id)
        code = (
            "SFTP_OPERATION_ALREADY_FINALIZED"
            if record is not None
            else "SFTP_OPERATION_NOT_FOUND"
        )
        raise ManualSftpError(code, "The remote upload operation is not active.")

    async def _fail_upload(
        self,
        state: _UploadState,
        error: ManualSftpError,
        *,
        remove_temp: bool,
    ) -> None:
        """尝试确定性清理后持久化失败回执。"""

        receipt = _terminal(
            state.operation_id,
            "failed",
            error.error_code,
            "The remote upload failed.",
        )
        await self._close_upload(state, remove_temp=remove_temp, receipt=receipt)

    async def _close_upload(
        self,
        state: _UploadState,
        *,
        remove_temp: bool,
        receipt: OperationTerminalProjection,
    ) -> None:
        """移除活动所有权，再按句柄、临时文件、通道顺序关闭。"""

        # 1. 先移除活动操作，防止新的分块继续使用待清理资源。
        self._active.pop(state.operation_id, None)
        # 2. 按写句柄、临时文件、通道顺序尝试全部清理，并保存首个失败。
        first_error: BaseException | None = None
        try:
            await _close_handle(state)
        except BaseException as exc:
            first_error = exc
        if remove_temp:
            try:
                await _metadata_await(
                    state.lease.client.remove(state.temp_path.encode("utf-8"))
                )
            except asyncssh.SFTPNoSuchFile:
                pass
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            await state.lease.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        # 3. 全部清理成功才写确定终态；否则持久化 cleanup_required 并抛出错误。
        if first_error is None:
            terminal_state = receipt.state
            self._put_upload_state(state, terminal_state, receipt)
            return
        cleanup_receipt = _terminal(
            state.operation_id,
            "cleanup_required",
            "SFTP_UPLOAD_CLEANUP_REQUIRED",
            "The remote upload temporary file requires manual cleanup.",
            recovery_id=state.operation_id,
        )
        self._put_upload_state(state, "cleanup_required", cleanup_receipt)
        raise ManualSftpError(
            "SFTP_UPLOAD_CLEANUP_REQUIRED",
            "The remote upload temporary file requires manual cleanup.",
            operation_state="cleanup_required",
        ) from first_error

    def _put_upload_state(
        self,
        state: _UploadState,
        record_state: str,
        receipt: OperationTerminalProjection | None = None,
    ) -> None:
        """持久化一次完整明文上传状态转换。"""

        self._operations.put(
            RemoteOperationRecord(
                operation_id=state.operation_id,
                kind="upload",
                state=record_state,
                connection_id=state.lease.owner.connection_id,
                connection_profile_version=state.lease.owner.connection_profile_version,
                host_label=state.lease.owner.host_label,
                target_host_key_fingerprint=state.lease.owner.target_host_key_fingerprint,
                jump_connection_id=state.lease.owner.jump_connection_id,
                jump_profile_version=state.lease.owner.jump_profile_version,
                jump_host_key_fingerprint=state.lease.owner.jump_host_key_fingerprint,
                remote_path=state.target_path,
                temp_path=state.temp_path,
                expected_sha256=state.source_sha256,
                target_snapshot=state.target_snapshot,
                terminal_receipt=receipt,
                created_at=state.created_at,
            )
        )


class DownloadManager:
    """管理拉取式远程读取，并在成功终态前验证源文件。"""

    def __init__(self, channels: SftpChannelFactory) -> None:
        """绑定独立通道创建入口并初始化操作注册表。"""

        self._channels = channels
        self._active: dict[UUID, _DownloadState] = {}
        self._finalized: set[UUID] = set()

    async def begin(
        self, *, operation_id: UUID, ssh_session_id: UUID, path: str
    ) -> DownloadReady:
        """打开拉取句柄前计算普通源文件哈希并冻结状态。"""

        # 1. 区分活动重复请求和已终结操作，禁止复用操作标识。
        if operation_id in self._active:
            raise ManualSftpError(
                "SFTP_OPERATION_DUPLICATE",
                "The manual SFTP operation ID cannot be reused.",
            )
        if operation_id in self._finalized:
            raise ManualSftpError(
                "SFTP_OPERATION_ALREADY_FINALIZED",
                "The manual SFTP operation ID is already finalized.",
            )
        # 2. 校验并冻结普通源文件的元数据、大小与哈希，再打开读取句柄。
        source_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            snapshot = await _snapshot(
                lease.client, source_path, include_hash=True
            )
            if snapshot.entry_type != "file" or snapshot.sha256 is None:
                raise ManualSftpError(
                    "SFTP_NOT_A_REGULAR_FILE",
                    "The selected remote entry is not a regular file.",
                )
            if snapshot.size is None:
                raise ManualSftpError(
                    "SFTP_ATTRIBUTES_INCOMPLETE",
                    "The remote file size is required for download.",
                )
            handle = await _metadata_await(
                lease.client.open(source_path.encode("utf-8"), "rb")
            )
        except BaseException:
            await lease.close()
            raise
        # 3. 把通道和句柄交给下载状态管理，返回冻结源信息。
        state = _DownloadState(
            operation_id=operation_id,
            lease=lease,
            handle=handle,
            source_path=source_path,
            source_snapshot=snapshot,
            expected_sequence=0,
            expected_offset=0,
            digest=hashlib.sha256(),
            eof=snapshot.size == 0,
        )
        self._active[operation_id] = state
        return DownloadReady(
            operation_id=operation_id,
            path=source_path,
            snapshot=snapshot,
            sha256=snapshot.sha256,
            byte_count=snapshot.size,
            next_sequence=0,
            next_offset=0,
        )

    async def read_chunk(
        self, operation_id: UUID, *, sequence: int, offset: int
    ) -> DownloadChunk:
        """返回且仅返回一个顺序、有界的原始二进制分块。"""

        # 1. 要求活动下载尚未 EOF，且请求序号和偏移量精确匹配。
        state = self._require_download(operation_id)
        if state.eof:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_OPERATION_ALREADY_FINALIZED",
                "The remote download has no additional chunk.",
            )
        if sequence != state.expected_sequence or offset != state.expected_offset:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_PROTOCOL_SEQUENCE_INVALID",
                "The download sequence or offset is invalid.",
            )
        try:
            # 2. 在单分块截止时间内读取，拒绝非法类型、超限或意外空读。
            async with asyncio.timeout(CHUNK_REQUEST_TIMEOUT_SECONDS):
                chunk = await state.handle.read(SFTP_CHUNK_BYTES)
        except TimeoutError as exc:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_TRANSFER_TIMEOUT", "The download chunk made no progress."
            ) from exc
        if not isinstance(chunk, bytes) or len(chunk) > SFTP_CHUNK_BYTES:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_REMOTE_READ_INVALID",
                "The remote file returned an invalid read chunk.",
            )
        if not chunk:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_DOWNLOAD_VERIFICATION_FAILED",
                "The remote file ended before its frozen size.",
            )
        # 3. 保存本块起点，再推进哈希与下一位置，按冻结大小判断 EOF。
        current_sequence = state.expected_sequence
        current_offset = state.expected_offset
        state.expected_sequence += 1
        state.expected_offset += len(chunk)
        state.digest.update(chunk)
        expected_size = state.source_snapshot.size
        state.eof = expected_size is not None and state.expected_offset >= expected_size
        return DownloadChunk(
            operation_id=operation_id,
            sequence=current_sequence,
            offset=current_offset,
            data=chunk,
            next_offset=state.expected_offset,
            eof=state.eof,
        )

    async def finish(self, operation_id: UUID) -> OperationTerminalProjection:
        """要求已达 EOF、流哈希匹配且最终元数据未变。"""

        # 1. 验证 EOF、总字节数和流式哈希，未完整下载不得成功。
        state = self._require_download(operation_id)
        expected_size = state.source_snapshot.size
        if (
            not state.eof
            or expected_size is None
            or state.expected_offset != expected_size
            or state.digest.hexdigest() != state.source_snapshot.sha256
        ):
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_DOWNLOAD_VERIFICATION_FAILED",
                "The downloaded bytes failed source verification.",
            )
        # 2. 重新读取源元数据，与最初快照比较，拒绝下载期间的源变化。
        final = await _snapshot(
            state.lease.client, state.source_path, include_hash=False
        )
        initial_without_hash = state.source_snapshot.model_copy(update={"sha256": None})
        if final != initial_without_hash:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_TARGET_CHANGED", "The remote download source changed."
            )
        # 3. 构建可信成功回执并关闭读取资源，然后返回结果。
        receipt = _terminal(
            operation_id,
            "succeeded",
            None,
            "The remote download source was verified.",
            sha256=state.source_snapshot.sha256,
            byte_count=state.expected_offset,
        )
        await self._close_download(state)
        return receipt

    async def abort(self, operation_id: UUID) -> OperationTerminalProjection:
        """仅关闭远程读取句柄和通道。"""

        state = self._require_download(operation_id)
        receipt = _terminal(
            operation_id, "cancelled", None, "The remote download was cancelled."
        )
        await self._close_download(state)
        return receipt

    async def close_all(self) -> None:
        """关闭全部活动远程下载，同时保留首个失败。"""

        first_error: BaseException | None = None
        for state in tuple(self._active.values()):
            try:
                await self._close_download(state)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _require_download(self, operation_id: UUID) -> _DownloadState:
        """返回活动下载，或区分已终结与未知 ID。"""

        state = self._active.get(operation_id)
        if state is not None:
            return state
        code = (
            "SFTP_OPERATION_ALREADY_FINALIZED"
            if operation_id in self._finalized
            else "SFTP_OPERATION_NOT_FOUND"
        )
        raise ManualSftpError(code, "The remote download operation is not active.")

    async def _close_download(self, state: _DownloadState) -> None:
        """移除注册表所有权并确定性关闭读取资源。"""

        self._active.pop(state.operation_id, None)
        self._finalized.add(state.operation_id)
        first_error: BaseException | None = None
        if state.handle is not None:
            try:
                await _metadata_await(state.handle.close())
            except BaseException as exc:
                first_error = exc
            finally:
                state.handle = None
        try:
            await state.lease.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise ManualSftpError(
                "SFTP_DOWNLOAD_CLEANUP_FAILED",
                "The remote download resources could not be closed.",
            ) from first_error


async def _snapshot(
    client: Any, path: str, *, include_hash: bool
) -> TransferSnapshot:
    """捕获不跟随链接元数据，并按需强制计算普通文件哈希。"""

    try:
        attrs = await _metadata_await(client.lstat(path.encode("utf-8")))
    except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
        return TransferSnapshot(
            path=path,
            exists=False,
            entry_type=None,
            size=None,
            mtime_ns=None,
            sha256=None,
        )
    entry = remote_entry(path, attrs)
    snapshot = TransferSnapshot(
        path=path,
        exists=True,
        entry_type=entry.entry_type,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        sha256=None,
    )
    if not include_hash or entry.entry_type != "file":
        return snapshot
    digest, byte_count = await _hash_file(client, path)
    final_attrs = await _metadata_await(client.lstat(path.encode("utf-8")))
    final_entry = remote_entry(path, final_attrs)
    final_snapshot = TransferSnapshot(
        path=path,
        exists=True,
        entry_type=final_entry.entry_type,
        size=final_entry.size,
        mtime_ns=final_entry.mtime_ns,
        sha256=None,
    )
    if final_snapshot != snapshot or (
        snapshot.size is not None and snapshot.size != byte_count
    ):
        raise ManualSftpError(
            "SFTP_TARGET_CHANGED", "The remote file changed while being hashed."
        )
    return snapshot.model_copy(update={"sha256": digest})


async def _hash_file(client: Any, path: str) -> tuple[str, int]:
    """通过有界无进展读取计算完整普通文件哈希。"""

    digest = hashlib.sha256()
    byte_count = 0
    handle = await _metadata_await(client.open(path.encode("utf-8"), "rb"))
    async with handle:
        while True:
            try:
                async with asyncio.timeout(NO_PROGRESS_TIMEOUT_SECONDS):
                    chunk = await handle.read(HASH_CHUNK_BYTES)
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
    return digest.hexdigest(), byte_count


async def _close_handle(state: _UploadState) -> None:
    """在验证或清理前只关闭一次上传句柄。"""

    if state.handle is not None:
        handle = state.handle
        state.handle = None
        await _metadata_await(handle.close())


async def _metadata_await(awaitable: Any) -> Any:
    """应用固定单请求截止时间，不重试远程 I/O。"""

    try:
        async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
            return await awaitable
    except TimeoutError as exc:
        raise ManualSftpError(
            "SFTP_OPERATION_TIMEOUT",
            "The SFTP metadata or mutation request timed out.",
        ) from exc


def _upload_temp_path(target_path: str, operation_id: UUID) -> str:
    """在最终目标旁创建不可猜测的独占上传临时文件。"""

    parent = posixpath.dirname(target_path) or "/"
    return posixpath.join(parent, f".harness-shell-upload-{operation_id}.part")


def _terminal(
    operation_id: UUID,
    state: str,
    error_code: str | None,
    message: str,
    *,
    sha256: str | None = None,
    byte_count: int | None = None,
    recovery_id: UUID | None = None,
) -> OperationTerminalProjection:
    """构建严格有界的终态投影。"""

    return OperationTerminalProjection(
        operation_id=operation_id,
        state=state,
        error_code=error_code,
        message=message,
        sha256=sha256,
        byte_count=byte_count,
        recovery_id=recovery_id,
    )


def _utc_now() -> str:
    """为明文记录返回稳定的 RFC 3339 UTC 时间戳。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
