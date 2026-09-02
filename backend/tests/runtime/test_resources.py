from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_shell_sidecar.runtime import (
    RuntimeInitializationFailure,
    RuntimeInitializeRequest,
    RuntimePhase,
)
from harness_shell_sidecar.runtime.resources import RuntimeResources
from harness_shell_sidecar.storage import AuditLedger, RuntimeDatabase


def initialize_request(tmp_path: Path) -> RuntimeInitializeRequest:
    """创建只用于当前测试数据库的严格运行时初始化请求。"""

    return RuntimeInitializeRequest(
        app_version="0.1.0",
        runtime_db_path=(tmp_path / "runtime.sqlite3").resolve(),
        runtime_data_key_b64=base64.b64encode(b"d" * 32).decode("ascii"),
        audit_hmac_key_b64=base64.b64encode(b"a" * 32).decode("ascii"),
        heartbeat_interval_ms=5_000,
        heartbeat_timeout_ms=15_000,
    )


async def discard_event(_event: dict[str, object]) -> None:
    """为不关注事件内容的资源测试提供异步事件出口。"""


def test_runtime_models_define_the_shared_http_lifecycle_boundary(
    tmp_path: Path,
) -> None:
    request = initialize_request(tmp_path)

    assert request.runtime_data_key() == b"d" * 32
    assert request.audit_hmac_key() == b"a" * 32
    assert [phase.value for phase in RuntimePhase] == [
        "LIVE_NOT_INITIALIZED",
        "INITIALIZING",
        "READY",
        "DRAINING",
        "CONVERGING",
        "CLOSING",
        "STOPPED",
        "FAILED",
    ]


def test_runtime_resources_initialize_all_handlers_and_shutdown_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        resources = RuntimeResources.initialize(
            initialize_request(tmp_path), discard_event
        )

        assert resources.state is RuntimePhase.READY
        for operation in (
            "connections.list",
            "host_key.inspect",
            "ssh.connect",
            "pty.open",
            "manual_sftp.list.begin",
            "agent.api_configs.list",
            "agent.turn.run",
        ):
            assert resources.dispatcher.handles(operation)

        await resources.shutdown()
        await resources.shutdown()

        assert resources.state is RuntimePhase.STOPPED
        assert resources.runtime_data_key_zeroized is True
        assert resources.audit_hmac_key_zeroized is True

    asyncio.run(scenario())


def test_runtime_resources_initialization_failure_closes_partial_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        AuditLedger,
        "verify_chain",
        lambda _ledger: SimpleNamespace(valid=False),
    )
    request = initialize_request(tmp_path)

    with pytest.raises(
        RuntimeInitializationFailure,
        match="audit chain verification failed",
    ):
        RuntimeResources.initialize(request, discard_event)

    database = RuntimeDatabase.open(request.runtime_db_path)
    database.close()


def test_runtime_resources_preserve_first_cleanup_error_and_run_later_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        resources = RuntimeResources.initialize(
            initialize_request(tmp_path), discard_event
        )
        order: list[str] = []

        original_dispatcher_close = resources.dispatcher.close
        original_manual_sftp_close = resources.manual_sftp_service.close_all
        original_ssh_close = resources.ssh_runtime.close_all
        original_trace_flush = resources.trace_provider.force_flush
        original_trace_shutdown = resources.trace_provider.shutdown
        original_record_zeroize = resources.record_store.zeroize
        original_audit_zeroize = resources.audit_ledger.zeroize
        original_database_close = resources.database.close

        async def close_dispatcher() -> None:
            order.append("dispatcher")
            await original_dispatcher_close()

        async def fail_pty() -> None:
            order.append("pty")
            raise OSError("PTY cleanup failed")

        async def close_manual_sftp() -> None:
            order.append("manual_sftp")
            await original_manual_sftp_close()

        async def close_ssh() -> None:
            order.append("ssh")
            await original_ssh_close()

        def flush_trace() -> bool:
            order.append("trace.flush")
            return original_trace_flush()

        def shutdown_trace() -> None:
            order.append("trace.shutdown")
            original_trace_shutdown()

        def zeroize_records() -> None:
            order.append("records.zeroize")
            original_record_zeroize()

        def zeroize_audit() -> None:
            order.append("audit.zeroize")
            original_audit_zeroize()

        def close_database() -> None:
            order.append("database")
            original_database_close()

        monkeypatch.setattr(resources.dispatcher, "close", close_dispatcher)
        monkeypatch.setattr(resources.pty_manager, "close_all", fail_pty)
        monkeypatch.setattr(
            resources.manual_sftp_service,
            "close_all",
            close_manual_sftp,
        )
        monkeypatch.setattr(resources.ssh_runtime, "close_all", close_ssh)
        monkeypatch.setattr(resources.trace_provider, "force_flush", flush_trace)
        monkeypatch.setattr(resources.trace_provider, "shutdown", shutdown_trace)
        monkeypatch.setattr(resources.record_store, "zeroize", zeroize_records)
        monkeypatch.setattr(resources.audit_ledger, "zeroize", zeroize_audit)
        monkeypatch.setattr(resources.database, "close", close_database)

        with pytest.raises(OSError, match="PTY cleanup failed"):
            await resources.shutdown()

        assert order == [
            "dispatcher",
            "pty",
            "manual_sftp",
            "ssh",
            "trace.flush",
            "trace.shutdown",
            "records.zeroize",
            "audit.zeroize",
            "database",
        ]
        assert resources.state is RuntimePhase.FAILED
        assert resources.runtime_data_key_zeroized is True
        assert resources.audit_hmac_key_zeroized is True

    asyncio.run(scenario())
