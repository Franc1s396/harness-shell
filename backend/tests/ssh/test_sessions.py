from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import asyncssh
import pytest

from harness_shell_sidecar.connections import ConnectionProfileInput, ConnectionRepository
from harness_shell_sidecar.ssh.errors import SshRuntimeError
from harness_shell_sidecar.ssh.host_keys import candidate_from_key
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import AuditLedger, LocalTraceStore, RuntimeDatabase
from harness_shell_sidecar.telemetry import build_local_tracer_provider


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class FailingChildChannel(FakeConnection):
    async def wait_closed(self) -> None:
        self.waited = True
        raise OSError("child close failed")


@dataclass
class FakeConnector:
    host_key: object
    failures: list[Exception]
    attempts: int = 0
    connection: FakeConnection | None = None

    async def __call__(self, host: str, port: int, **options):
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        client = options["client_factory"]()
        client.validate_host_public_key(host, host, port, self.host_key)
        self.connection = FakeConnection()
        return self.connection


def setup_runtime(tmp_path: Path, connector: FakeConnector):
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    repo = ConnectionRepository(database)
    value = repo.create(
        ConnectionProfileInput(
            display_name="retry",
            group_name=None,
            host="retry.example",
            port=22,
            username="deploy",
            auth_kind="password",
            credential_id=uuid4(),
            passphrase_credential_id=None,
            proxy_jump_id=None,
            favorite=False,
        )
    )
    repo.trust_first_host_key(
        candidate_from_key(
            value.connection_id, value.host, value.port, connector.host_key
        )
    )
    return database, value, SshRuntime(repo, connector=connector)


def test_retryable_pre_auth_connection_failure_retries_exactly_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        connector = FakeConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            [OSError("tcp failed")],
        )
        database, value, runtime = setup_runtime(tmp_path, connector)
        try:
            status = await runtime.connect(value.connection_id, password=b"secret")
            assert status.state == "READY"
            assert connector.attempts == 2
        finally:
            await runtime.close_all()
            database.close()

    asyncio.run(scenario())


def test_child_close_failure_still_closes_target_and_jump_transports() -> None:
    async def scenario() -> None:
        sessions = SshSessionRegistry()
        target = FakeConnection()
        jump = FakeConnection()
        child = FailingChildChannel()
        session = sessions.register(uuid4(), target, jump)
        session.child_channels.add(child)

        with pytest.raises(OSError, match="child close failed"):
            await sessions.close(session.ssh_session_id)

        assert child.closed is True
        assert child.waited is True
        assert target.closed is True
        assert target.waited is True
        assert jump.closed is True
        assert jump.waited is True
        assert len(sessions) == 0

    asyncio.run(scenario())


def test_close_all_continues_after_an_earlier_session_fails() -> None:
    async def scenario() -> None:
        sessions = SshSessionRegistry()
        first_target = FakeConnection()
        first = sessions.register(uuid4(), first_target)
        first.child_channels.add(FailingChildChannel())
        second_target = FakeConnection()
        sessions.register(uuid4(), second_target)

        with pytest.raises(OSError, match="child close failed"):
            await sessions.close_all()

        assert first_target.closed is True
        assert second_target.closed is True
        assert second_target.waited is True
        assert len(sessions) == 0

    asyncio.run(scenario())


def test_each_connect_attempt_is_recorded_in_audit_and_trace(tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = FakeConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            [OSError("tcp failed")],
        )
        database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
        repo = ConnectionRepository(database)
        value = repo.create(
            ConnectionProfileInput(
                display_name="observed",
                group_name=None,
                host="observed.example",
                port=22,
                username="deploy",
                auth_kind="password",
                credential_id=uuid4(),
                passphrase_credential_id=None,
                proxy_jump_id=None,
                favorite=False,
            )
        )
        repo.trust_first_host_key(
            candidate_from_key(
                value.connection_id, value.host, value.port, connector.host_key
            )
        )
        audit = AuditLedger(database, b"a" * 32)
        trace_provider = build_local_tracer_provider(LocalTraceStore(database))
        tracer = trace_provider.get_tracer("harness_shell_sidecar.ssh.test")
        runtime = SshRuntime(
            repo,
            connector=connector,
            audit_ledger=audit,
            tracer=tracer,
        )
        try:
            await runtime.connect(value.connection_id, password=b"secret")
            entries = database.execute(
                "SELECT body_json FROM audit_entries "
                "WHERE event_type = 'ssh.connect.attempt' ORDER BY sequence"
            ).fetchall()
            assert len(entries) == 2
            assert '"attempt":"1"' in entries[0][0]
            assert '"outcome":"failed"' in entries[0][0]
            assert '"attempt":"2"' in entries[1][0]
            assert '"outcome":"succeeded"' in entries[1][0]
            trace_provider.force_flush()
            spans = database.execute(
                "SELECT name, attributes_json FROM trace_spans ORDER BY started_at"
            ).fetchall()
            assert [span[0] for span in spans] == [
                "ssh.connect.attempt",
                "ssh.connect.attempt",
            ]
            assert '"ssh.connect.attempt":1' in spans[0][1]
            assert '"ssh.connect.outcome":"failed"' in spans[0][1]
            assert '"ssh.connect.attempt":2' in spans[1][1]
            assert '"ssh.connect.outcome":"succeeded"' in spans[1][1]
        finally:
            await runtime.close_all()
            trace_provider.shutdown()
            audit.zeroize()
            database.close()

    asyncio.run(scenario())


def test_profile_change_after_secret_resolution_blocks_network_io(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        connector = FakeConnector(asyncssh.generate_private_key("ssh-ed25519"), [])
        database, value, runtime = setup_runtime(tmp_path, connector)
        repo = runtime._repository
        try:
            repo.update(
                value.connection_id,
                ConnectionProfileInput(
                    display_name="changed",
                    group_name=None,
                    host="changed.example",
                    port=22,
                    username="deploy",
                    auth_kind="password",
                    credential_id=uuid4(),
                    passphrase_credential_id=None,
                    proxy_jump_id=None,
                    favorite=False,
                ),
            )
            with pytest.raises(SshRuntimeError) as raised:
                await runtime.connect(
                    value.connection_id,
                    password=b"secret",
                    expected_profile_updated_at=value.updated_at,
                )
            assert raised.value.error_code == "CONNECTION_PROFILE_CHANGED"
            assert connector.attempts == 0
        finally:
            database.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        asyncssh.PermissionDenied("denied"),
        SshRuntimeError(
            "HOST_KEY_CHANGED",
            node="host_key",
            recoverable=False,
            remote_state="pre_auth",
        ),
    ],
)
def test_authentication_and_host_key_failures_never_retry(
    tmp_path: Path, failure: Exception
) -> None:
    async def scenario() -> None:
        connector = FakeConnector(
            asyncssh.generate_private_key("ssh-ed25519"), [failure]
        )
        database, value, runtime = setup_runtime(tmp_path, connector)
        try:
            with pytest.raises(SshRuntimeError):
                await runtime.connect(value.connection_id, password=b"secret")
            assert connector.attempts == 1
            assert len(runtime.sessions) == 0
        finally:
            database.close()

    asyncio.run(scenario())
