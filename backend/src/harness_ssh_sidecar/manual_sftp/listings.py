"""远程目录列表的有界、带序号校验的所有权管理。"""

from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import asyncssh

from .channels import SftpChannelFactory, SftpChannelLease
from .errors import ManualSftpError, map_typed_sftp_status
from .models import ListingBatch, RemoteEntry
from .paths import join_remote_path, validate_remote_path


LISTING_BATCH_SIZE = 200
MAX_DIRECTORY_ENTRIES = 50_000
LISTING_BATCH_TIMEOUT_SECONDS = 30
MAX_JS_SAFE_INTEGER = 2**53 - 1
MAX_UINT64 = 2**64 - 1


@dataclass(slots=True)
class _ListingCursor:
    """拥有一个迭代器、通道、预期序号和已观察数量。"""

    #: 跨 typed HTTP 边界返回的公共游标标识。
    listing_id: UUID
    #: 已校验的目录绝对路径。
    path: str
    #: 拥有底层 SFTP 客户端的租约。
    lease: SftpChannelLease
    #: 通过 aclose 关闭句柄的 AsyncSSH 目录迭代器。
    iterator: Any
    #: 下次显式 next 请求接受的序号。
    expected_sequence: int
    #: 已返回给调用方的条目数。
    observed_entry_count: int


class ListingManager:
    """拥有全部短生命周期列表游标及其独立 SFTP 通道。"""

    def __init__(self, channels: SftpChannelFactory) -> None:
        """绑定通道工厂并创建空游标注册表。"""

        self._channels = channels
        self._cursors: dict[UUID, _ListingCursor] = {}

    async def begin(self, ssh_session_id: UUID, path: str) -> ListingBatch:
        """打开游标并返回序号为零的首批条目。"""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            iterator = lease.client.scandir(remote_path.encode("utf-8"))
        except asyncssh.SFTPPermissionDenied as exc:
            try:
                await lease.close()
            except BaseException:
                pass
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote directory listing.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        except BaseException as primary_error:
            try:
                await lease.close()
            except BaseException:
                pass
            raise primary_error
        cursor = _ListingCursor(
            listing_id=uuid4(),
            path=remote_path,
            lease=lease,
            iterator=iterator,
            expected_sequence=0,
            observed_entry_count=0,
        )
        try:
            batch = await self._read_batch(cursor, sequence=0)
        except BaseException as primary_error:
            try:
                await self._close_cursor(cursor)
            except BaseException:
                pass
            raise primary_error
        if not batch.done:
            self._cursors[cursor.listing_id] = cursor
        return batch

    async def next(self, listing_id: UUID, sequence: int) -> ListingBatch:
        """仅返回预期的下一批，或关闭非法游标。"""

        cursor = self._cursors.get(listing_id)
        if cursor is None:
            raise ManualSftpError(
                "SFTP_LISTING_NOT_FOUND", "The directory listing is not active."
            )
        if sequence != cursor.expected_sequence:
            self._cursors.pop(listing_id, None)
            await self._close_cursor(cursor)
            raise ManualSftpError(
                "SFTP_PROTOCOL_SEQUENCE_INVALID",
                "The directory listing sequence is invalid.",
            )
        try:
            batch = await self._read_batch(cursor, sequence=sequence)
        except BaseException as primary_error:
            self._cursors.pop(listing_id, None)
            try:
                await self._close_cursor(cursor)
            except BaseException:
                pass
            raise primary_error
        if batch.done:
            self._cursors.pop(listing_id, None)
        return batch

    async def close(self, listing_id: UUID) -> None:
        """关闭活动游标及其通道。"""

        cursor = self._cursors.pop(listing_id, None)
        if cursor is None:
            raise ManualSftpError(
                "SFTP_LISTING_NOT_FOUND", "The directory listing is not active."
            )
        await self._close_cursor(cursor)

    async def close_all(self) -> None:
        """关闭全部游标，尝试所有清理后再暴露失败。"""

        cursors = tuple(self._cursors.values())
        self._cursors.clear()
        first_error: BaseException | None = None
        for cursor in cursors:
            try:
                await self._close_cursor(cursor)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _read_batch(
        self, cursor: _ListingCursor, *, sequence: int
    ) -> ListingBatch:
        """读取有界批次，并关闭自然结束的游标。"""

        # 1. 建立本批缓冲，在固定超时内读取并验证真实目录条目。
        entries: list[RemoteEntry] = []
        done = False
        try:
            async with asyncio.timeout(LISTING_BATCH_TIMEOUT_SECONDS):
                while len(entries) < LISTING_BATCH_SIZE:
                    if cursor.observed_entry_count == MAX_DIRECTORY_ENTRIES:
                        while True:
                            try:
                                lookahead = await anext(cursor.iterator)
                            except StopAsyncIteration:
                                done = True
                                break
                            if _remote_entry(cursor.path, lookahead) is not None:
                                raise ManualSftpError(
                                    "SFTP_DIRECTORY_ENTRY_LIMIT_EXCEEDED",
                                    "The remote directory contains more than 50000 entries.",
                                )
                        if done:
                            break
                    try:
                        item = await anext(cursor.iterator)
                    except StopAsyncIteration:
                        done = True
                        break
                    entry = _remote_entry(cursor.path, item)
                    if entry is None:
                        continue
                    entries.append(entry)
                    cursor.observed_entry_count += 1
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_LISTING_TIMEOUT", "The remote directory listing timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote directory listing.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc

        # 2. 仅在整批读取成功后推进下一序号；自然结束时关闭游标和通道。
        cursor.expected_sequence = sequence + 1
        if done:
            await self._close_cursor(cursor)
        # 3. 返回有界批次及完成标志，让客户端按精确序号继续。
        return ListingBatch(
            listing_id=cursor.listing_id,
            path=cursor.path,
            entries=tuple(entries),
            next_sequence=cursor.expected_sequence,
            done=done,
            observed_entry_count=cursor.observed_entry_count,
            complete=done,
        )

    @staticmethod
    async def _close_cursor(cursor: _ListingCursor) -> None:
        """先关闭目录迭代器，再关闭其通道租约。"""

        first_error: BaseException | None = None
        close_iterator = getattr(cursor.iterator, "aclose", None)
        if close_iterator is not None:
            try:
                await close_iterator()
            except BaseException as exc:
                first_error = exc
        try:
            await cursor.lease.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


def remote_entry(path: str, attrs: Any, *, link_target: str | None = None) -> RemoteEntry:
    """使用 AsyncSSH 公共属性构建严格的不跟随链接元数据。"""

    remote_path = validate_remote_path(path)
    name = remote_path.rstrip("/").rsplit("/", 1)[-1] or "/"
    mode = getattr(attrs, "permissions", None)
    if type(mode) is not int or mode < 0:
        raise ManualSftpError(
            "SFTP_ATTRIBUTES_INCOMPLETE", "Remote POSIX permissions are missing."
        )
    entry_type = _entry_type(mode)
    raw_size = getattr(attrs, "size", None)
    size: int | None = None
    if entry_type == "file" and raw_size is not None:
        if type(raw_size) is not int or not 0 <= raw_size <= MAX_JS_SAFE_INTEGER:
            raise ManualSftpError(
                "SFTP_FILE_SIZE_UNSUPPORTED",
                "The remote file size is outside the supported range.",
            )
        size = raw_size
    return RemoteEntry(
        name=name,
        path=remote_path,
        entry_type=entry_type,
        size=size,
        mode=mode,
        mtime_ns=_mtime_ns(attrs),
        link_target=link_target,
    )


def _remote_entry(directory: str, item: Any) -> RemoteEntry | None:
    """严格解码文件名并映射不跟随链接的列表属性。"""

    raw_name = getattr(item, "filename", None)
    try:
        if isinstance(raw_name, bytes):
            name = raw_name.decode("utf-8", "strict")
        elif isinstance(raw_name, str):
            raw_name.encode("utf-8", "strict")
            name = raw_name
        else:
            raise UnicodeError("filename is not text or bytes")
    except UnicodeError as exc:
        raise ManualSftpError(
            "SFTP_FILENAME_ENCODING_UNSUPPORTED",
            "A remote filename is not valid UTF-8.",
        ) from exc
    if name in {".", ".."}:
        return None
    return remote_entry(join_remote_path(directory, name), item.attrs)


def _entry_type(mode: int) -> str:
    """映射 POSIX 文件类型位，不跟随符号链接。"""

    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _mtime_ns(attrs: Any) -> str | None:
    """将精确整数秒和纳秒编码为 uint64 十进制字符串。"""

    seconds = getattr(attrs, "mtime", None)
    if seconds is None:
        return None
    nanoseconds = getattr(attrs, "mtime_ns", None)
    nanoseconds = 0 if nanoseconds is None else nanoseconds
    if (
        type(seconds) is not int
        or type(nanoseconds) is not int
        or seconds < 0
        or not 0 <= nanoseconds < 1_000_000_000
    ):
        raise ManualSftpError(
            "SFTP_ATTRIBUTES_INVALID", "Remote modification time is invalid."
        )
    value = seconds * 1_000_000_000 + nanoseconds
    if value > MAX_UINT64:
        raise ManualSftpError(
            "SFTP_ATTRIBUTES_INVALID", "Remote modification time is invalid."
        )
    return str(value)
