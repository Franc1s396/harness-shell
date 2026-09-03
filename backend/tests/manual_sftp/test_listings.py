"""Manual SFTP channel, listing, and metadata service tests."""

from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

import asyncssh
import pytest

from harness_shell_sidecar.manual_sftp.channels import SftpChannelFactory
from harness_shell_sidecar.manual_sftp import channels as channels_module
from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp.listings import ListingManager
from harness_shell_sidecar.manual_sftp.service import ManualSftpService
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry


SESSION_CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000111")


def test_channel_open_has_a_fixed_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingConnection:
        async def start_sftp_client(self, **_options):
            await asyncio.Event().wait()

    async def scenario() -> None:
        sessions = SshSessionRegistry()
        owner = sessions.register(
            SESSION_CONNECTION_ID,
            HangingConnection(),
            connection_profile_version=1,
            host_label="demo-host",
            target_host_key_fingerprint="SHA256:test-target",
        )
        monkeypatch.setattr(channels_module, "CHANNEL_OPERATION_TIMEOUT_SECONDS", 0)

        with pytest.raises(ManualSftpError) as raised:
            await SftpChannelFactory(sessions).open(owner.ssh_session_id)

        assert raised.value.error_code == "SFTP_OPERATION_TIMEOUT"

    asyncio.run(scenario())


def attrs(
    mode: int,
    *,
    size: int | None = None,
    mtime: int | None = 1_770_000_000,
    mtime_ns: int | None = 123,
):
    """Build the public AsyncSSH attribute shape used by the service."""

    return SimpleNamespace(
        permissions=mode,
        size=size,
        mtime=mtime,
        mtime_ns=mtime_ns,
    )


@dataclass(slots=True)
class FakeName:
    """One deterministic SFTP directory entry."""

    filename: str | bytes
    attrs: object


class FakeRemoteFile:
    """Bounded in-memory remote file used by SHA-256 tests."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
        self.closed = True
        return False

    async def read(self, length: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + length]
        self._offset += len(chunk)
        return chunk


class FakeSftpClient:
    """Expose only the public AsyncSSH client methods consumed by Task 2."""

    version = 3

    def __init__(self, entries: list[FakeName] | None = None) -> None:
        self.entries = entries or []
        self.exited = False
        self.waited = False
        self.stat_value = attrs(stat.S_IFREG | 0o644, size=7)
        self.stat_after_read = self.stat_value
        self.lstat_calls = 0
        self.payload = b"payload"

    async def getcwd(self) -> bytes:
        return b"/home/demo"

    def scandir(self, path: bytes):
        assert isinstance(path, bytes)

        async def iterator():
            for entry in self.entries:
                yield entry

        return iterator()

    async def lstat(self, path: bytes):
        assert isinstance(path, bytes)
        self.lstat_calls += 1
        return self.stat_after_read if self.lstat_calls > 1 else self.stat_value

    async def readlink(self, path: bytes) -> bytes:
        assert isinstance(path, bytes)
        return b"../target.txt"

    async def open(self, path: bytes, mode: str) -> FakeRemoteFile:
        assert isinstance(path, bytes)
        assert mode == "rb"
        return FakeRemoteFile(self.payload)

    def exit(self) -> None:
        self.exited = True

    async def wait_closed(self) -> None:
        self.waited = True


class FakeConnection:
    """Return one isolated SFTP client per requested channel."""

    def __init__(self, clients: list[FakeSftpClient]) -> None:
        self.clients = clients

    async def start_sftp_client(self, **options) -> FakeSftpClient:
        assert options == {"path_encoding": None}
        return self.clients.pop(0)


def sessions_with(*clients: FakeSftpClient):
    """Register one live SSH session backed by deterministic SFTP clients."""

    sessions = SshSessionRegistry()
    owner = sessions.register(
        SESSION_CONNECTION_ID,
        FakeConnection(list(clients)),
        connection_profile_version=1,
        host_label="demo-host",
        target_host_key_fingerprint="SHA256:test-target",
    )
    return sessions, owner


def test_listing_batches_two_hundred_entries_and_closes_channel() -> None:
    async def scenario() -> None:
        client = FakeSftpClient(
            [
                FakeName(
                    f"entry-{index}.txt",
                    attrs(stat.S_IFREG | 0o644, size=index),
                )
                for index in range(201)
            ]
        )
        sessions, owner = sessions_with(client)
        listings = ListingManager(SftpChannelFactory(sessions))

        first = await listings.begin(owner.ssh_session_id, "/home/demo")
        assert len(first.entries) == 200
        assert first.next_sequence == 1
        assert first.done is False
        assert client in owner.child_channels

        final = await listings.next(first.listing_id, 1)
        assert len(final.entries) == 1
        assert final.done is True
        assert final.complete is True
        assert final.observed_entry_count == 201
        assert client not in owner.child_channels
        assert client.exited is True
        assert client.waited is True

    asyncio.run(scenario())


def test_listing_ignores_server_dot_entries_without_counting_them() -> None:
    async def scenario() -> None:
        client = FakeSftpClient(
            [
                FakeName(".", attrs(stat.S_IFDIR | 0o755)),
                FakeName("..", attrs(stat.S_IFDIR | 0o755)),
                FakeName("data.txt", attrs(stat.S_IFREG | 0o644, size=4)),
            ]
        )
        sessions, owner = sessions_with(client)
        listings = ListingManager(SftpChannelFactory(sessions))

        batch = await listings.begin(owner.ssh_session_id, "/home/demo")

        assert [entry.name for entry in batch.entries] == ["data.txt"]
        assert batch.observed_entry_count == 1
        assert batch.done is True

    asyncio.run(scenario())


def test_listing_sequence_mismatch_closes_cursor_and_channel() -> None:
    async def scenario() -> None:
        client = FakeSftpClient(
            [FakeName(f"entry-{index}", attrs(stat.S_IFREG | 0o644, size=1)) for index in range(201)]
        )
        sessions, owner = sessions_with(client)
        listings = ListingManager(SftpChannelFactory(sessions))
        first = await listings.begin(owner.ssh_session_id, "/home/demo")

        with pytest.raises(ManualSftpError, match="SFTP_PROTOCOL_SEQUENCE_INVALID"):
            await listings.next(first.listing_id, 9)
        assert client not in owner.child_channels

    asyncio.run(scenario())


def test_listing_detects_entry_limit_and_unsupported_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.listings.MAX_DIRECTORY_ENTRIES", 2
        )
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.listings.LISTING_BATCH_SIZE", 2
        )
        limit_client = FakeSftpClient(
            [
                FakeName("one", attrs(stat.S_IFREG | 0o644, size=1)),
                FakeName("two", attrs(stat.S_IFREG | 0o644, size=1)),
                FakeName("three", attrs(stat.S_IFREG | 0o644, size=1)),
            ]
        )
        encoding_client = FakeSftpClient(
            [FakeName(b"\xff", attrs(stat.S_IFREG | 0o644, size=1))]
        )
        sessions, owner = sessions_with(limit_client, encoding_client)
        listings = ListingManager(SftpChannelFactory(sessions))

        first = await listings.begin(owner.ssh_session_id, "/home/demo")
        with pytest.raises(
            ManualSftpError, match="SFTP_DIRECTORY_ENTRY_LIMIT_EXCEEDED"
        ):
            await listings.next(first.listing_id, first.next_sequence)
        with pytest.raises(
            ManualSftpError, match="SFTP_FILENAME_ENCODING_UNSUPPORTED"
        ):
            await listings.begin(owner.ssh_session_id, "/home/demo")
        assert owner.child_channels == set()

    asyncio.run(scenario())


def test_listing_limit_lookahead_ignores_dot_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only real entries may cause the bounded-listing look-ahead to overflow."""

    async def scenario() -> None:
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.listings.MAX_DIRECTORY_ENTRIES", 2
        )
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.listings.LISTING_BATCH_SIZE", 2
        )
        client = FakeSftpClient(
            [
                FakeName("one", attrs(stat.S_IFREG | 0o644, size=1)),
                FakeName("two", attrs(stat.S_IFREG | 0o644, size=1)),
                FakeName(".", attrs(stat.S_IFDIR | 0o755)),
                FakeName("..", attrs(stat.S_IFDIR | 0o755)),
            ]
        )
        sessions, owner = sessions_with(client)
        listings = ListingManager(SftpChannelFactory(sessions))

        first = await listings.begin(owner.ssh_session_id, "/home/demo")
        final = await listings.next(first.listing_id, first.next_sequence)

        assert [entry.name for entry in first.entries] == ["one", "two"]
        assert final.entries == ()
        assert final.done is True
        assert final.observed_entry_count == 2

    asyncio.run(scenario())


def test_browse_metadata_listing_and_hash_map_permission_denial_to_stable_code() -> None:
    """Each read boundary must expose AsyncSSH permission rejection as one safe code."""

    class BrowseDeniedClient(FakeSftpClient):
        async def getcwd(self) -> bytes:
            raise asyncssh.SFTPPermissionDenied("denied")

    class LstatDeniedClient(FakeSftpClient):
        async def lstat(self, path: bytes):
            raise asyncssh.SFTPPermissionDenied("denied")

    class ReadlinkDeniedClient(FakeSftpClient):
        async def readlink(self, path: bytes) -> bytes:
            raise asyncssh.SFTPPermissionDenied("denied")

    class HashDeniedClient(FakeSftpClient):
        async def open(self, path: bytes, mode: str):
            raise asyncssh.SFTPPermissionDenied("denied")

    class ListingDeniedClient(FakeSftpClient):
        def scandir(self, path: bytes):
            async def iterator():
                raise asyncssh.SFTPPermissionDenied("denied")
                yield None

            return iterator()

    class ScandirCallDeniedClient(FakeSftpClient):
        def scandir(self, path: bytes):
            raise asyncssh.SFTPPermissionDenied("denied")

    async def scenario() -> None:
        clients = (
            BrowseDeniedClient(),
            LstatDeniedClient(),
            ReadlinkDeniedClient(),
            HashDeniedClient(),
            ListingDeniedClient(),
            ScandirCallDeniedClient(),
        )
        sessions, owner = sessions_with(*clients)
        service = ManualSftpService(sessions, records=object(), event_listener=_event)
        requests = (
            lambda: service.open(owner.ssh_session_id),
            lambda: service.lstat(owner.ssh_session_id, "/home/demo/data.txt"),
            lambda: service.readlink(owner.ssh_session_id, "/home/demo/link"),
            lambda: service.sha256(owner.ssh_session_id, "/home/demo/data.txt"),
            lambda: service.list_begin(owner.ssh_session_id, "/home/demo"),
            lambda: service.list_begin(owner.ssh_session_id, "/home/demo"),
        )
        for request in requests:
            with pytest.raises(ManualSftpError) as raised:
                await request()
            assert raised.value.error_code == "SFTP_PERMISSION_DENIED"

    asyncio.run(scenario())


def test_listing_permission_denial_survives_iterator_and_lease_cleanup_failures() -> None:
    """Cleanup runs but cannot replace the primary permission-denied domain failure."""

    class PermissionDeniedIterator:
        """Raise typed denial from iteration and a distinct failure during close."""

        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise asyncssh.SFTPPermissionDenied("denied")

        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("iterator cleanup failed")

    class CleanupFailureClient(FakeSftpClient):
        def __init__(self) -> None:
            super().__init__()
            self.iterator = PermissionDeniedIterator()

        def scandir(self, path: bytes):
            return self.iterator

        async def wait_closed(self) -> None:
            self.waited = True
            raise RuntimeError("lease cleanup failed")

    async def scenario() -> None:
        client = CleanupFailureClient()
        sessions, owner = sessions_with(client)
        listings = ListingManager(SftpChannelFactory(sessions))

        with pytest.raises(ManualSftpError) as raised:
            await listings.begin(owner.ssh_session_id, "/home/demo")
        assert raised.value.error_code == "SFTP_PERMISSION_DENIED"
        assert client.iterator.closed is True
        assert client.exited is True
        assert client.waited is True
        assert owner.child_channels == set()

    asyncio.run(scenario())


def test_metadata_uses_lstat_preserves_exact_mtime_and_reads_link_explicitly() -> None:
    async def scenario() -> None:
        lstat_client = FakeSftpClient()
        lstat_client.stat_value = attrs(
            stat.S_IFLNK | 0o777,
            size=99,
            mtime=1_770_000_000,
            mtime_ns=456,
        )
        link_client = FakeSftpClient()
        link_client.stat_value = lstat_client.stat_value
        sessions, owner = sessions_with(lstat_client, link_client)
        service = ManualSftpService(sessions, records=object(), event_listener=_event)

        entry = await service.lstat(owner.ssh_session_id, "/home/demo/link")
        assert entry.entry_type == "symlink"
        assert entry.size is None
        assert entry.mtime_ns == "1770000000000000456"
        assert entry.link_target is None

        linked = await service.readlink(owner.ssh_session_id, "/home/demo/link")
        assert linked.entry_type == "symlink"
        assert linked.link_target == "../target.txt"
        assert owner.child_channels == set()

    asyncio.run(scenario())


def test_hash_rechecks_snapshot_and_honors_cancellation() -> None:
    async def scenario() -> None:
        changed = FakeSftpClient()
        changed.stat_after_read = attrs(
            stat.S_IFREG | 0o644, size=7, mtime=1_770_000_001, mtime_ns=123
        )
        cancelled_client = FakeSftpClient()
        sessions, owner = sessions_with(changed, cancelled_client)
        service = ManualSftpService(sessions, records=object(), event_listener=_event)

        with pytest.raises(ManualSftpError, match="SFTP_TARGET_CHANGED"):
            await service.sha256(owner.ssh_session_id, "/home/demo/data.txt")

        cancelled = asyncio.Event()
        cancelled.set()
        with pytest.raises(ManualSftpError, match="SFTP_REQUEST_CANCELLED"):
            await service.sha256(
                owner.ssh_session_id, "/home/demo/data.txt", cancelled=cancelled
            )
        assert owner.child_channels == set()

    asyncio.run(scenario())


def test_hash_uses_a_fresh_60_second_window_for_each_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hash has only per-read progress windows, never one total deadline."""

    class TimeoutProbe:
        """Record every timeout context without advancing wall-clock time."""

        def __init__(self) -> None:
            self.windows: list[int | float] = []

        def __call__(self, delay: int | float):
            self.windows.append(delay)
            return self

        async def __aenter__(self) -> "TimeoutProbe":
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
            return False

    async def scenario() -> None:
        payload = b"h" * (2 * 65_536 + 1)
        client = FakeSftpClient()
        client.payload = payload
        client.stat_value = attrs(stat.S_IFREG | 0o644, size=len(payload))
        client.stat_after_read = client.stat_value
        sessions, owner = sessions_with(client)
        service = ManualSftpService(sessions, records=object(), event_listener=_event)
        probe = TimeoutProbe()
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.service.asyncio.timeout", probe
        )

        result = await service.sha256(owner.ssh_session_id, "/home/demo/data.txt")

        assert result.byte_count == len(payload)
        assert probe.windows == [15, 15, 60, 60, 60, 60, 15, 15]
        assert owner.child_channels == set()

    asyncio.run(scenario())


def test_metadata_timeout_closes_the_sftp_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingClient(FakeSftpClient):
        """Never complete lstat before the configured metadata deadline."""

        async def lstat(self, path: bytes):
            assert isinstance(path, bytes)
            await asyncio.Event().wait()

    async def scenario() -> None:
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.service.METADATA_TIMEOUT_SECONDS",
            0.001,
        )
        client = BlockingClient()
        sessions, owner = sessions_with(client)
        service = ManualSftpService(sessions, records=object(), event_listener=_event)

        with pytest.raises(ManualSftpError, match="SFTP_OPERATION_TIMEOUT"):
            await service.lstat(owner.ssh_session_id, "/home/demo/data.txt")
        assert owner.child_channels == set()
        assert client.exited is True
        assert client.waited is True

    asyncio.run(scenario())


async def _event(_payload: dict) -> None:
    """Accept no-op service events in Task 2 tests."""


def test_service_close_all_attempts_listing_upload_and_download_owners() -> None:
    class CloseProbe:
        """Record close order and optionally fail after recording."""

        def __init__(self, label: str, order: list[str], *, fail: bool = False) -> None:
            self.label = label
            self.order = order
            self.fail = fail

        async def close_all(self) -> None:
            self.order.append(self.label)
            if self.fail:
                raise OSError(f"{self.label} close failed")

    async def scenario() -> None:
        sessions = SshSessionRegistry()
        service = ManualSftpService(sessions, records=object(), event_listener=_event)
        order: list[str] = []
        service._listings = CloseProbe("listings", order, fail=True)
        service._uploads = CloseProbe("uploads", order)
        service._downloads = CloseProbe("downloads", order)

        with pytest.raises(OSError, match="listings close failed"):
            await service.close_all()
        assert order == ["listings", "uploads", "downloads"]

    asyncio.run(scenario())
