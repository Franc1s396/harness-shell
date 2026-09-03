"""No-follow mutation and recursive tombstone tests."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncssh
import pytest

from harness_shell_sidecar.manual_sftp.channels import SftpChannelFactory
from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp.mutations import MutationManager
from harness_shell_sidecar.manual_sftp.models import TransferSnapshot
from harness_shell_sidecar.manual_sftp.operation_store import ManualSftpOperationStore
from harness_shell_sidecar.manual_sftp.transfers import UploadManager
from harness_shell_sidecar.ssh.sessions import SshSession, SshSessionRegistry
from harness_shell_sidecar.storage import PlaintextRecordStore, RuntimeDatabase


CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000401")
ROOT = "/home/demo/tree"


@dataclass(slots=True)
class Node:
    """One fake no-follow filesystem entry."""

    kind: str
    payload: bytes = b""
    target: str | None = None
    mtime: int = 1_770_000_000


class FakeName:
    """Public AsyncSSH scandir item shape."""

    def __init__(self, filename: str, attrs) -> None:
        self.filename = filename.encode("utf-8")
        self.attrs = attrs


class FakeReadHandle:
    """Expose the bounded async read surface used by real snapshot hashing."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    async def __aenter__(self) -> "FakeReadHandle":
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
        return False

    async def read(self, length: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + length]
        self._offset += len(chunk)
        return chunk


class FakeMutationClient:
    """Expose public SFTP mutation APIs over one shared tree."""

    version = 3

    def __init__(self, remote: "FakeMutationRemote") -> None:
        self.remote = remote

    async def lstat(self, path: bytes):
        if self.remote.lstat_error is not None:
            raise self.remote.lstat_error
        value = path.decode("utf-8")
        node = self.remote.nodes.get(value)
        if node is None:
            raise asyncssh.SFTPNoSuchFile("missing")
        mode = {
            "file": stat.S_IFREG | 0o644,
            "directory": stat.S_IFDIR | 0o755,
            "symlink": stat.S_IFLNK | 0o777,
        }[node.kind]
        return SimpleNamespace(
            permissions=mode,
            size=len(node.payload) if node.kind == "file" else None,
            mtime=node.mtime,
            mtime_ns=0,
        )

    async def open(self, path: bytes, mode: str) -> FakeReadHandle:
        assert mode == "rb"
        node = self.remote.nodes.get(path.decode("utf-8"))
        if node is None or node.kind != "file":
            raise asyncssh.SFTPNoSuchFile("missing")
        return FakeReadHandle(node.payload)

    def scandir(self, path: bytes):
        directory = path.decode("utf-8")
        self.remote.scandir_paths.append(directory)

        async def iterator():
            if self.remote.emit_dot_entries:
                yield FakeName(".", SimpleNamespace())
                yield FakeName("..", SimpleNamespace())
            prefix = directory.rstrip("/") + "/"
            children = []
            for candidate, node in self.remote.nodes.items():
                if candidate.startswith(prefix):
                    suffix = candidate[len(prefix) :]
                    if suffix and "/" not in suffix:
                        children.append((suffix, node))
            for name, _node in sorted(children):
                yield FakeName(name, await self.lstat((prefix + name).encode("utf-8")))

        return iterator()

    async def readlink(self, path: bytes) -> bytes:
        node = self.remote.nodes[path.decode("utf-8")]
        assert node.target is not None
        return node.target.encode("utf-8")

    async def mkdir(self, path: bytes) -> None:
        self.remote.nodes[path.decode("utf-8")] = Node("directory")

    async def statvfs(self, path: bytes):
        """Return a deterministic filesystem ID for rename preflight."""

        value = path.decode("utf-8")
        fsid = (
            2
            if any(value.startswith(root) for root in self.remote.cross_device_roots)
            else 1
        )
        return SimpleNamespace(fsid=fsid)

    async def rename(self, source: bytes, target: bytes, *, flags: int) -> None:
        source_path = source.decode("utf-8")
        target_path = target.decode("utf-8")
        self.remote.rename_calls.append((source_path, target_path, flags))
        if self.remote.rename_error is not None:
            raise self.remote.rename_error
        moved = {
            target_path + path[len(source_path) :]: node
            for path, node in self.remote.nodes.items()
            if path == source_path or path.startswith(source_path + "/")
        }
        for path in list(self.remote.nodes):
            if path == source_path or path.startswith(source_path + "/"):
                del self.remote.nodes[path]
        self.remote.nodes.update(moved)
        if self.remote.lstat_error_after_rename is not None:
            self.remote.lstat_error = self.remote.lstat_error_after_rename
        if self.remote.mutate_after_atomic_rename:
            self.remote.nodes[target_path + "/late.txt"] = Node("file", b"late")

    async def remove(self, path: bytes) -> None:
        value = path.decode("utf-8")
        self.remote.deleted_paths.append(value)
        if self.remote.delete_error is not None:
            raise self.remote.delete_error
        self.remote.nodes.pop(value)

    async def rmdir(self, path: bytes) -> None:
        value = path.decode("utf-8")
        self.remote.deleted_paths.append(value)
        if self.remote.delete_error is not None:
            raise self.remote.delete_error
        self.remote.nodes.pop(value)

    def exit(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class FakeConnection:
    """Create isolated clients over shared fake remote state."""

    def __init__(self, remote: "FakeMutationRemote") -> None:
        self.remote = remote

    async def start_sftp_client(self, **options):
        assert options == {"path_encoding": None}
        return FakeMutationClient(self.remote)


class FakeMutationRemote:
    """Shared tree plus evidence that forbidden fallbacks never occurred."""

    def __init__(self) -> None:
        self.nodes = {
            ROOT: Node("directory"),
            ROOT + "/inside.txt": Node("file", b"inside"),
            ROOT + "/link": Node("symlink", target="/outside/secret"),
            "/outside": Node("directory"),
            "/outside/secret": Node("file", b"secret"),
        }
        self.scandir_paths: list[str] = []
        self.deleted_paths: list[str] = []
        self.rename_calls: list[tuple[str, str, int]] = []
        self.copy_calls: list[tuple] = []
        self.remove_calls: list[str] = []
        self.mutate_after_atomic_rename = False
        self.rename_error: BaseException | None = None
        self.cross_device_roots: set[str] = set()
        self.emit_dot_entries = False
        self.lstat_error: BaseException | None = None
        self.lstat_error_after_rename: BaseException | None = None
        self.delete_error: BaseException | None = None


async def event(_payload: dict) -> None:
    """Accept mutation progress events in focused unit tests."""


def manager(tmp_path: Path, remote: FakeMutationRemote):
    """Build one mutation manager and its live session/database owners."""

    database, owner, mutations, _operations = manager_with_operations(tmp_path, remote)
    return database, owner, mutations


def manager_with_operations(
    tmp_path: Path, remote: FakeMutationRemote
) -> tuple[RuntimeDatabase, SshSession, MutationManager, ManualSftpOperationStore]:
    """Build a mutation manager while exposing its plaintext operation store."""

    database = RuntimeDatabase.open_plaintext(
        (tmp_path / "runtime.sqlite3").resolve()
    )
    operations = ManualSftpOperationStore(PlaintextRecordStore(database))
    sessions = SshSessionRegistry()
    owner = sessions.register(
        CONNECTION_ID,
        FakeConnection(remote),
        connection_profile_version=1,
        host_label="demo-host",
        target_host_key_fingerprint="SHA256:test-target",
    )
    mutations = MutationManager(SftpChannelFactory(sessions), operations, event)
    return database, owner, mutations, operations


def file_snapshot(path: str, payload: bytes) -> TransferSnapshot:
    """Mirror the domain's no-follow file snapshot at a known instant."""

    return TransferSnapshot(
        path=path,
        exists=True,
        entry_type="file",
        size=len(payload),
        mtime_ns="1770000000000000000",
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_upload_preflight_returns_hash_inside_existing_regular_target_metadata(
    tmp_path: Path,
) -> None:
    """The Python contract includes a regular target hash before Rust revalidates it."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        target_path = ROOT + "/existing-target.txt"
        target_payload = b"existing target"
        remote.nodes[target_path] = Node("file", target_payload)
        database = RuntimeDatabase.open_plaintext(
            (tmp_path / "runtime.sqlite3").resolve()
        )
        sessions = SshSessionRegistry()
        owner = sessions.register(
            CONNECTION_ID,
            FakeConnection(remote),
            connection_profile_version=1,
            host_label="demo-host",
            target_host_key_fingerprint="SHA256:test-target",
        )
        uploads = UploadManager(
            SftpChannelFactory(sessions),
            ManualSftpOperationStore(PlaintextRecordStore(database)),
        )
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, target_path)
            assert snapshot == file_snapshot(target_path, target_payload)
        finally:
            database.close()

    asyncio.run(scenario())


def test_ordinary_file_rename_and_remove_succeed_with_fresh_hash_snapshots(
    tmp_path: Path,
) -> None:
    """Exercise the concrete Python mutation domain, including its hash rechecks."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        source_path = ROOT + "/inside.txt"
        target_path = ROOT + "/renamed.txt"
        source_payload = remote.nodes[source_path].payload
        database, owner, mutations = manager(tmp_path, remote)
        try:
            renamed = await mutations.rename(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                source_path=source_path,
                target_path=target_path,
                overwrite=False,
                source_snapshot=file_snapshot(source_path, source_payload),
                target_snapshot=TransferSnapshot(
                    path=target_path,
                    exists=False,
                    entry_type=None,
                    size=None,
                    mtime_ns=None,
                    sha256=None,
                ),
            )
            assert renamed.state == "succeeded"
            assert target_path in remote.nodes
            assert remote.rename_calls == [(source_path, target_path, 0)]

            removed = await mutations.remove(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                path=target_path,
                expected_snapshot=file_snapshot(target_path, source_payload),
            )
            assert removed.state == "succeeded"
            assert target_path not in remote.nodes
        finally:
            database.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["source", "target"])
def test_changed_rename_snapshot_fails_before_remote_mutation(
    tmp_path: Path, changed: str
) -> None:
    """MutationManager compares both current snapshots before it records or dispatches rename."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        source_path = ROOT + "/inside.txt"
        target_path = ROOT + "/target.txt"
        source_payload = remote.nodes[source_path].payload
        target_payload = b"target before"
        remote.nodes[target_path] = Node("file", target_payload)
        expected_source = file_snapshot(source_path, source_payload)
        expected_target = file_snapshot(target_path, target_payload)
        if changed == "source":
            remote.nodes[source_path].payload = b"source changed"
        else:
            remote.nodes[target_path].payload = b"target changed"
        database, owner, mutations = manager(tmp_path, remote)
        try:
            with pytest.raises(ManualSftpError) as raised:
                await mutations.rename(
                    operation_id=uuid4(),
                    ssh_session_id=owner.ssh_session_id,
                    source_path=source_path,
                    target_path=target_path,
                    overwrite=True,
                    source_snapshot=expected_source,
                    target_snapshot=expected_target,
                )
            assert raised.value.error_code == "SFTP_TARGET_CHANGED"
            assert remote.rename_calls == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_mkdir_target_exists_leaves_no_nonterminal_operation_record(
    tmp_path: Path,
) -> None:
    """A pre-dispatch target-exists result must not become a recovery item."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        target_path = ROOT + "/existing"
        remote.nodes[target_path] = Node("directory")
        database, owner, mutations, operations = manager_with_operations(tmp_path, remote)
        operation_id = uuid4()
        try:
            with pytest.raises(ManualSftpError) as raised:
                await mutations.mkdir(
                    operation_id=operation_id,
                    ssh_session_id=owner.ssh_session_id,
                    parent_path=ROOT,
                    name="existing",
                )
            assert raised.value.error_code == "SFTP_TARGET_EXISTS"
            assert operations.get(operation_id) is None
            assert operations.list_non_terminal() == ()
        finally:
            database.close()

    asyncio.run(scenario())


def test_nonempty_directory_remove_leaves_no_nonterminal_operation_record(
    tmp_path: Path,
) -> None:
    """A proven non-empty directory fails before mutation intent is persisted."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        database, owner, mutations, operations = manager_with_operations(tmp_path, remote)
        operation_id = uuid4()
        try:
            with pytest.raises(ManualSftpError) as raised:
                await mutations.remove(
                    operation_id=operation_id,
                    ssh_session_id=owner.ssh_session_id,
                    path=ROOT,
                    expected_snapshot=TransferSnapshot(
                        path=ROOT,
                        exists=True,
                        entry_type="directory",
                        size=None,
                        mtime_ns="1770000000000000000",
                        sha256=None,
                    ),
                )
            assert raised.value.error_code == "SFTP_DIRECTORY_NOT_EMPTY"
            assert operations.get(operation_id) is None
            assert operations.list_non_terminal() == ()
            assert remote.deleted_paths == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_delete_never_enters_symlink_target(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeMutationRemote()
        database, owner, mutations = manager(tmp_path, remote)
        try:
            plan = await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=uuid4()
            )
            assert plan.symlink_count == 1
            assert "/outside/secret" not in remote.scandir_paths
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_preflight_uses_only_60_second_progress_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each manifest step receives its own progress window without a total timer."""

    class TimeoutProbe:
        """Record manifest timeout scopes while letting each fake I/O step proceed."""

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
        remote = FakeMutationRemote()
        database, owner, mutations = manager(tmp_path, remote)
        probe = TimeoutProbe()
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.mutations.asyncio.timeout", probe
        )
        try:
            plan = await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=uuid4()
            )
            assert plan.complete is True
            assert probe.windows == [15, 60, 60, 60, 60, 60, 60, 60, 15]
            assert owner.child_channels == set()
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_delete_preflight_rejects_a_reused_caller_operation_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeMutationRemote()
        database, owner, mutations = manager(tmp_path, remote)
        operation_id = uuid4()
        try:
            await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=operation_id
            )
            with pytest.raises(ManualSftpError, match="SFTP_OPERATION_DUPLICATE"):
                await mutations.delete_preflight(
                    owner.ssh_session_id, ROOT, operation_id=operation_id
                )
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_delete_stops_when_tombstone_manifest_changes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeMutationRemote()
        database, owner, mutations = manager(tmp_path, remote)
        try:
            plan = await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=uuid4()
            )
            remote.mutate_after_atomic_rename = True
            result = await mutations.delete_execute(plan.delete_plan_id)
            assert result.state == "cleanup_required"
            assert remote.deleted_paths == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_delete_pre_isolation_failure_consumes_plan_and_terminalizes_record(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeMutationRemote()
        database, owner, mutations, operations = manager_with_operations(tmp_path, remote)
        try:
            plan = await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=uuid4()
            )
            remote.nodes[ROOT].mtime += 1

            with pytest.raises(ManualSftpError) as raised:
                await mutations.delete_execute(plan.delete_plan_id)

            assert raised.value.error_code == "SFTP_TARGET_CHANGED"
            operation = operations.get(plan.operation_id)
            assert operation is not None
            assert operation.state == "failed"
            persisted_plan = operations.get_delete_plan(plan.delete_plan_id)
            assert persisted_plan is not None
            assert persisted_plan.consumed is True
            assert persisted_plan.terminal_receipt is not None
            assert remote.rename_calls == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_delete_permission_failures_are_typed_and_durable(
    tmp_path: Path,
) -> None:
    """Permission denial at every delete-execute phase must persist recovery then fail."""

    async def scenario() -> None:
        for phase in ("initial", "rescan", "deleting"):
            remote = FakeMutationRemote()
            database, owner, mutations = manager(tmp_path / phase, remote)
            try:
                plan = await mutations.delete_preflight(
                    owner.ssh_session_id, ROOT, operation_id=uuid4()
                )
                if phase == "initial":
                    remote.lstat_error = asyncssh.SFTPPermissionDenied("denied")
                elif phase == "rescan":
                    remote.lstat_error_after_rename = asyncssh.SFTPPermissionDenied(
                        "denied"
                    )
                else:
                    remote.delete_error = asyncssh.SFTPPermissionDenied("denied")

                with pytest.raises(ManualSftpError) as raised:
                    await mutations.delete_execute(plan.delete_plan_id)
                assert raised.value.error_code == "SFTP_PERMISSION_DENIED"
                persisted = mutations._operations.get(plan.operation_id)
                assert persisted is not None
                assert persisted.terminal_receipt is not None
                assert persisted.terminal_receipt.error_code == "SFTP_PERMISSION_DENIED"
            finally:
                database.close()

    asyncio.run(scenario())


def test_recursive_delete_rescan_unknown_failure_is_durable_and_explicit(
    tmp_path: Path,
) -> None:
    """An isolated tombstone must persist recovery before a rescan failure escapes."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        database, owner, mutations = manager(tmp_path, remote)
        try:
            plan = await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=uuid4()
            )
            remote.lstat_error_after_rename = RuntimeError("rescan failed")

            with pytest.raises(ManualSftpError) as raised:
                await mutations.delete_execute(plan.delete_plan_id)

            assert raised.value.error_code == "SFTP_TOMBSTONE_CLEANUP_REQUIRED"
            persisted = mutations._operations.get(plan.operation_id)
            assert persisted is not None
            assert persisted.state == "cleanup_required"
            assert persisted.terminal_receipt is not None
            assert (
                persisted.terminal_receipt.error_code
                == "SFTP_TOMBSTONE_CLEANUP_REQUIRED"
            )
            assert persisted.terminal_receipt.recovery_id == plan.operation_id
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_delete_unknown_cleanup_failure_is_durable_and_raises(
    tmp_path: Path,
) -> None:
    """An unexpected deletion failure cannot be returned as a normal terminal result."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        remote.delete_error = RuntimeError("unexpected remote failure")
        database, owner, mutations = manager(tmp_path, remote)
        try:
            plan = await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=uuid4()
            )
            with pytest.raises(ManualSftpError) as raised:
                await mutations.delete_execute(plan.delete_plan_id)
            assert raised.value.error_code == "SFTP_TOMBSTONE_CLEANUP_REQUIRED"
            persisted = mutations._operations.get(plan.operation_id)
            assert persisted is not None
            assert persisted.state == "cleanup_required"
        finally:
            database.close()

    asyncio.run(scenario())


def test_cross_device_rename_never_copies_or_deletes(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeMutationRemote()
        remote.rename_error = OSError(errno.EXDEV, "cross device")
        database, owner, mutations = manager(tmp_path, remote)
        try:
            with pytest.raises(
                ManualSftpError, match="SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED"
            ):
                await mutations.rename(
                    operation_id=uuid4(),
                    ssh_session_id=owner.ssh_session_id,
                    source_path=ROOT + "/inside.txt",
                    target_path=ROOT + "/moved.txt",
                    overwrite=False,
                )
            assert remote.copy_calls == []
            assert remote.remove_calls == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_cross_device_statvfs_rejects_before_rename_dispatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeMutationRemote()
        remote.nodes["/separate"] = Node("directory")
        remote.cross_device_roots.add("/separate")
        database, owner, mutations = manager(tmp_path, remote)
        try:
            with pytest.raises(
                ManualSftpError, match="SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED"
            ):
                await mutations.rename(
                    operation_id=uuid4(),
                    ssh_session_id=owner.ssh_session_id,
                    source_path=ROOT + "/inside.txt",
                    target_path="/separate/moved.txt",
                    overwrite=False,
                )
            assert remote.rename_calls == []
            assert remote.copy_calls == []
            assert remote.remove_calls == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_remove_accepts_an_empty_directory_when_server_lists_dot_entries(
    tmp_path: Path,
) -> None:
    """AsyncSSH's real dot entries must not make an otherwise empty directory non-empty."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        empty_path = ROOT + "/empty"
        remote.nodes[empty_path] = Node("directory")
        remote.emit_dot_entries = True
        database, owner, mutations = manager(tmp_path, remote)
        try:
            terminal = await mutations.remove(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                path=empty_path,
                expected_snapshot=TransferSnapshot(
                    path=empty_path,
                    exists=True,
                    entry_type="directory",
                    size=None,
                    mtime_ns="1770000000000000000",
                    sha256=None,
                ),
            )
            assert terminal.state == "succeeded"
            assert empty_path in remote.deleted_paths
        finally:
            database.close()

    asyncio.run(scenario())


def test_mutation_snapshot_permission_denial_uses_the_stable_error_code(
    tmp_path: Path,
) -> None:
    """Permission denial before a mutation record exists remains a typed domain failure."""

    async def scenario() -> None:
        remote = FakeMutationRemote()
        remote.lstat_error = asyncssh.SFTPPermissionDenied("denied")
        database, owner, mutations = manager(tmp_path, remote)
        try:
            with pytest.raises(ManualSftpError) as raised:
                await mutations.mkdir(
                    operation_id=uuid4(),
                    ssh_session_id=owner.ssh_session_id,
                    parent_path=ROOT,
                    name="new-directory",
                )
            assert raised.value.error_code == "SFTP_PERMISSION_DENIED"
        finally:
            database.close()

    asyncio.run(scenario())


def test_recursive_delete_is_one_shot_bottom_up_and_manifest_is_plaintext(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeMutationRemote()
        database, owner, mutations = manager(tmp_path, remote)
        try:
            plan = await mutations.delete_preflight(
                owner.ssh_session_id, ROOT, operation_id=uuid4()
            )
            persisted_files = [database.path, database.path.with_name(database.path.name + "-wal")]
            persisted = b"".join(
                path.read_bytes() for path in persisted_files if path.exists()
            )
            assert ROOT.encode("utf-8") in persisted

            terminal = await mutations.delete_execute(plan.delete_plan_id)
            assert terminal.state == "succeeded"
            assert remote.rename_calls[0][2] == 0
            assert remote.deleted_paths[-1].endswith(".tombstone")
            assert all(not path.startswith(ROOT) for path in remote.nodes)
            deleted_once = list(remote.deleted_paths)

            repeated = await mutations.delete_execute(plan.delete_plan_id)
            assert repeated == terminal
            assert remote.deleted_paths == deleted_once
        finally:
            database.close()

    asyncio.run(scenario())
