"""Bounded, sequence-checked ownership for remote directory listings."""

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
    """Own one iterator, channel, expected sequence, and observed count."""

    #: Public cursor identifier returned across the typed HTTP boundary.
    listing_id: UUID
    #: Validated absolute directory path.
    path: str
    #: Lease which owns the underlying SFTP client.
    lease: SftpChannelLease
    #: AsyncSSH directory iterator whose handle closes through ``aclose``.
    iterator: Any
    #: Sequence accepted by the next explicit ``next`` request.
    expected_sequence: int
    #: Entries already returned to the caller.
    observed_entry_count: int


class ListingManager:
    """Own every short-lived listing cursor and its isolated SFTP channel."""

    def __init__(self, channels: SftpChannelFactory) -> None:
        """Bind the channel factory and create an empty cursor registry."""

        self._channels = channels
        self._cursors: dict[UUID, _ListingCursor] = {}

    async def begin(self, ssh_session_id: UUID, path: str) -> ListingBatch:
        """Open a cursor and return its first sequence-zero batch."""

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
        """Return exactly the expected next batch or close the invalid cursor."""

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
        """Close one active cursor and its channel."""

        cursor = self._cursors.pop(listing_id, None)
        if cursor is None:
            raise ManualSftpError(
                "SFTP_LISTING_NOT_FOUND", "The directory listing is not active."
            )
        await self._close_cursor(cursor)

    async def close_all(self) -> None:
        """Close all cursors, attempting every cleanup before surfacing failure."""

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
        """Read one bounded batch and close naturally completed cursors."""

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

        cursor.expected_sequence = sequence + 1
        if done:
            await self._close_cursor(cursor)
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
        """Close the directory iterator before closing its channel lease."""

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
    """Build strict no-follow metadata from public AsyncSSH attributes."""

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
    """Decode one filename strictly and map its no-follow listing attributes."""

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
    """Map POSIX file type bits without following symbolic links."""

    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _mtime_ns(attrs: Any) -> str | None:
    """Encode exact integer seconds and nanoseconds as one uint64 decimal string."""

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
