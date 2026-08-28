from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from harness_shell_sidecar.connections import ConnectionProfileInput, ConnectionRepository
from harness_shell_sidecar.remote_io import ArtifactStore
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.storage import (
    AuditLedger,
    EncryptedRecordStore,
    LocalTraceStore,
    RuntimeDatabase,
)
from harness_shell_sidecar.telemetry import build_local_tracer_provider


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
LAB_RUNTIME = WORKSPACE_ROOT / "tests" / "ssh_lab" / ".runtime"


def pytest_collection_modifyitems(items) -> None:
    if os.environ.get("HARNESS_RUN_SSH_INTEGRATION") == "1":
        return
    marker = pytest.mark.skip(reason="set HARNESS_RUN_SSH_INTEGRATION=1 after starting the SSH lab")
    for item in items:
        if "ssh_integration" in item.nodeid:
            item.add_marker(marker)


@dataclass(frozen=True)
class LabConfig:
    """从本地 SSH lab 清单与秘密文件加载的只读测试配置。"""

    #: 跳板容器可访问主机。
    jump_host: str
    #: 跳板 SSH 映射端口。
    jump_port: int
    #: 跳板登录用户名。
    jump_username: str
    #: 跳板测试密码。
    jump_password: str
    #: 启动脚本记录的跳板 Host Key 指纹。
    jump_host_fingerprint: str
    #: 目标容器可访问主机。
    target_host: str
    #: 目标 SSH 映射端口。
    target_port: int
    #: 目标登录用户名。
    target_username: str
    #: 目标测试密码。
    target_password: str
    #: 启动脚本记录的目标 Host Key 指纹。
    target_host_fingerprint: str
    #: 无口令测试私钥路径。
    unencrypted_private_key_path: Path
    #: 带口令测试私钥路径。
    encrypted_private_key_path: Path
    #: 加密测试私钥的口令。
    private_key_passphrase: str


@pytest.fixture(scope="session")
def lab() -> LabConfig:
    manifest_path = LAB_RUNTIME / "manifest.json"
    secrets_path = LAB_RUNTIME / "secrets.json"
    if not manifest_path.is_file() or not secrets_path.is_file():
        pytest.fail("SSH lab manifest is missing; run scripts/start-ssh-lab.ps1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
    return LabConfig(
        jump_host=manifest["jump_host"],
        jump_port=manifest["jump_port"],
        jump_username=manifest["jump_username"],
        jump_password=secrets["jump_password"],
        jump_host_fingerprint=manifest["jump_host_fingerprint"],
        target_host=manifest["target_host"],
        target_port=manifest["target_port"],
        target_username=manifest["target_username"],
        target_password=secrets["target_password"],
        target_host_fingerprint=manifest["target_host_fingerprint"],
        unencrypted_private_key_path=Path(manifest["unencrypted_private_key_path"]),
        encrypted_private_key_path=Path(manifest["encrypted_private_key_path"]),
        private_key_passphrase=secrets["private_key_passphrase"],
    )


class RuntimeContext:
    """拥有单个 SSH 集成测试所需全部本地运行时资源的夹具上下文。"""

    def __init__(self, path: Path) -> None:
        """创建隔离数据库、加密仓储、审计、Trace 和 SSH Runtime。"""

        self.database = RuntimeDatabase.open(path.resolve())  # 本测试隔离数据库。
        self.records = EncryptedRecordStore(self.database, b"m" * 32)  # 加密记录仓储。
        self.artifacts = ArtifactStore(self.database, self.records)  # 远端输出仓储。
        self.repository = ConnectionRepository(self.database)  # 连接与 Host Key 仓储。
        self.audit = AuditLedger(self.database, b"a" * 32)  # 测试审计链。
        # 仅写当前测试数据库的 Trace Provider。
        self.trace_provider = build_local_tracer_provider(
            LocalTraceStore(self.database)
        )
        self.runtime = SshRuntime(  # 连接真实 SSH lab 的运行时。
            self.repository,
            audit_ledger=self.audit,
            tracer=self.trace_provider.get_tracer("harness_shell_sidecar.ssh_integration"),
        )

    def create_profile(
        self,
        *,
        name: str,
        host: str,
        port: int,
        username: str,
        auth_kind: str = "password",
        proxy_jump_id=None,
    ):
        """在隔离仓储中创建一条测试连接配置。"""

        return self.repository.create(
            ConnectionProfileInput(
                display_name=name,
                group_name="integration",
                host=host,
                port=port,
                username=username,
                auth_kind=auth_kind,
                credential_id=uuid4(),
                passphrase_credential_id=uuid4() if auth_kind == "private_key" else None,
                proxy_jump_id=proxy_jump_id,
                favorite=False,
            )
        )

    async def close(self) -> None:
        """按依赖顺序关闭 SSH、遥测、Key 与数据库资源。"""

        await self.runtime.close_all()
        self.trace_provider.force_flush()
        self.trace_provider.shutdown()
        self.audit.zeroize()
        self.records.zeroize()
        self.database.close()


@pytest.fixture
def runtime_context(tmp_path: Path):
    context = RuntimeContext(tmp_path / "runtime.sqlite3")
    yield context


@pytest.fixture
def connect_proxy(runtime_context: RuntimeContext, lab: LabConfig):
    async def connect():
        jump = runtime_context.create_profile(
            name="jump",
            host=lab.jump_host,
            port=lab.jump_port,
            username=lab.jump_username,
        )
        target = runtime_context.create_profile(
            name="target",
            host=lab.target_host,
            port=lab.target_port,
            username=lab.target_username,
            proxy_jump_id=jump.connection_id,
        )
        jump_observation = await runtime_context.runtime.inspect_host_key(
            target.connection_id,
            jump_connection_id=jump.connection_id,
            expected_jump_profile_updated_at=jump.updated_at,
            jump_password=lab.jump_password.encode(),
        )
        assert jump_observation.host_key_candidate is not None
        assert jump_observation.host_key_candidate.fingerprint_sha256 == lab.jump_host_fingerprint
        runtime_context.repository.trust_first_host_key(jump_observation.host_key_candidate)

        target_observation = await runtime_context.runtime.inspect_host_key(
            target.connection_id,
            jump_connection_id=jump.connection_id,
            expected_jump_profile_updated_at=jump.updated_at,
            jump_password=lab.jump_password.encode(),
        )
        assert target_observation.host_key_candidate is not None
        assert target_observation.host_key_candidate.fingerprint_sha256 == lab.target_host_fingerprint
        runtime_context.repository.trust_first_host_key(target_observation.host_key_candidate)
        status = await runtime_context.runtime.connect(
            target.connection_id,
            password=lab.target_password.encode(),
            expected_profile_updated_at=target.updated_at,
            jump_connection_id=jump.connection_id,
            jump_password=lab.jump_password.encode(),
            expected_jump_profile_updated_at=jump.updated_at,
        )
        assert status.state == "READY"
        return jump, target, status

    return connect
