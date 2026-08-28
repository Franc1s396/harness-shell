"""Canonical AsyncSSH Host Key capture and comparison."""

from __future__ import annotations

import base64
from uuid import UUID

import asyncssh

from harness_shell_sidecar.connections import HostKeyCandidate, HostKeyRecord


class HostKeyObserved(Exception):
    """检查客户端截获到未经信任 Host Key 后用于中止握手的内部信号。"""

    def __init__(self, candidate: HostKeyCandidate) -> None:
        """保存规范化候选值，供上层进入显式确认流程。"""

        super().__init__("Host Key observed")
        self.candidate = candidate  # 本次握手实际观察到的规范化公钥信息。


class HostKeyMismatch(Exception):
    """远端 Host Key 与持久化信任记录不一致时的内部信号。"""

    def __init__(self, candidate: HostKeyCandidate) -> None:
        """保存不匹配的新候选值，供上层安全展示和审计。"""

        super().__init__("Host Key mismatch")
        self.candidate = candidate  # 当前远端实际返回的规范化公钥信息。


def candidate_from_key(
    connection_id: UUID,
    host: str,
    port: int,
    key: asyncssh.SSHKey,
) -> HostKeyCandidate:
    exported = key.export_public_key("openssh").rstrip(b"\r\n")
    return HostKeyCandidate(
        connection_id=connection_id,
        host=host,
        port=port,
        key_algorithm=key.get_algorithm(),
        fingerprint_sha256=key.get_fingerprint("sha256"),
        public_key_openssh_b64=base64.b64encode(exported).decode("ascii"),
    )


class InspectHostKeyClient(asyncssh.SSHClient):
    """只捕获远端 Host Key、绝不接受它继续认证的 AsyncSSH Client。"""

    def __init__(self, connection_id: UUID, host: str, port: int) -> None:
        """保存候选 Host Key 必须绑定的逻辑连接与实际端点。"""

        self._connection_id = connection_id  # 目标连接配置标识符。
        self._host = host  # 实际连接主机名或 IP 地址。
        self._port = port  # 实际连接 SSH 端口。

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        """规范化观察到的 Key 并通过内部信号中止握手。"""

        raise HostKeyObserved(
            candidate_from_key(
                self._connection_id, self._host, self._port, key
            )
        )


class VerifiedHostKeyClient(asyncssh.SSHClient):
    """仅接受与完整持久化信任记录精确匹配的 AsyncSSH Client。"""

    def __init__(
        self,
        connection_id: UUID,
        host: str,
        port: int,
        trusted: HostKeyRecord,
    ) -> None:
        """保存端点身份及必须精确匹配的信任记录。"""

        self._connection_id = connection_id  # 目标连接配置标识符。
        self._host = host  # 实际连接主机名或 IP 地址。
        self._port = port  # 实际连接 SSH 端口。
        self._trusted = trusted  # 用户已经确认并处于 active 状态的 Host Key。

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        """同时比较算法、指纹和公钥正文，任一不符即 fail closed。"""

        candidate = candidate_from_key(
            self._connection_id, self._host, self._port, key
        )
        if (
            candidate.key_algorithm == self._trusted.key_algorithm
            and candidate.fingerprint_sha256 == self._trusted.fingerprint_sha256
            and candidate.public_key_openssh_b64
            == self._trusted.public_key_openssh_b64
        ):
            return True
        raise HostKeyMismatch(candidate)


def empty_known_hosts() -> asyncssh.SSHKnownHosts:
    """Return a truthy, explicit empty store so AsyncSSH invokes our callback."""

    return asyncssh.import_known_hosts("")
