"""SQLite repositories for M2 connection profiles and host keys."""

from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timezone
from uuid import UUID, uuid4

from harness_shell_sidecar.storage import RuntimeDatabase

from .models import (
    ConnectionProfile,
    ConnectionProfileInput,
    HostKeyCandidate,
    HostKeyRecord,
)


class ConnectionRepositoryError(RuntimeError):
    """携带稳定业务错误码的连接仓储异常。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存对外错误码，同时沿用异常消息供日志与调试使用。"""

        super().__init__(message)
        self.error_code = error_code  # 可安全映射到 IPC 错误响应的稳定代码。
        self.safe_message = message  # 不包含连接凭据的具体失败原因。


class ConnectionRepository:
    """在运行时 SQLite 数据库中管理连接配置与 Host Key 历史。"""

    def __init__(self, database: RuntimeDatabase) -> None:
        """绑定由运行时统一拥有生命周期的数据库连接。"""

        self._database = database  # 当前 Sidecar 运行实例的数据库访问入口。

    def create(self, value: ConnectionProfileInput) -> ConnectionProfile:
        """校验 ProxyJump 后创建连接配置并回读持久化结果。"""

        connection_id = uuid4()
        now = _utc_now()
        self._validate_proxy(value.proxy_jump_id, connection_id)
        self._database.execute(
            """
            INSERT INTO connection_profiles(
                connection_id, display_name, group_name, host, port, username,
                auth_kind, credential_id, passphrase_credential_id,
                proxy_jump_id, favorite, created_at, updated_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _profile_parameters(connection_id, value, now, now),
        )
        profile = self.get(connection_id)
        if profile is None:
            raise ConnectionRepositoryError(
                "CONNECTION_PERSISTENCE_FAILED", "created profile was not found"
            )
        return profile

    def update(
        self, connection_id: UUID, value: ConnectionProfileInput
    ) -> ConnectionProfile:
        """以完整新配置替换已有连接，并保留创建时间。"""

        current = self.get(connection_id)
        if current is None:
            raise ConnectionRepositoryError(
                "CONNECTION_NOT_FOUND", "connection profile was not found"
            )
        if current.version == 2**53 - 1:
            raise ConnectionRepositoryError(
                "CONNECTION_VERSION_EXHAUSTED",
                "connection profile version is exhausted",
            )
        self._validate_proxy(value.proxy_jump_id, connection_id)
        cursor = self._database.execute(
            """
            UPDATE connection_profiles SET
                display_name = ?, group_name = ?, host = ?, port = ?,
                username = ?, auth_kind = ?, credential_id = ?,
                passphrase_credential_id = ?, proxy_jump_id = ?, favorite = ?,
                updated_at = ?, version = version + 1
            WHERE connection_id = ? AND version = ?
            """,
            (
                value.display_name,
                value.group_name,
                value.host,
                value.port,
                value.username,
                value.auth_kind,
                str(value.credential_id),
                _uuid_text(value.passphrase_credential_id),
                _uuid_text(value.proxy_jump_id),
                int(value.favorite),
                _utc_now(),
                str(connection_id),
                current.version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConnectionRepositoryError(
                "CONNECTION_PERSISTENCE_FAILED",
                "connection profile version changed during update",
            )
        updated = self.get(connection_id)
        if updated is None or updated.version != current.version + 1:
            raise ConnectionRepositoryError(
                "CONNECTION_PERSISTENCE_FAILED", "updated profile was not found"
            )
        return updated

    def get(self, connection_id: UUID) -> ConnectionProfile | None:
        """按唯一标识符读取连接配置；不存在时返回 None。"""

        row = self._database.execute(
            _PROFILE_SELECT + " WHERE connection_id = ?",
            (str(connection_id),),
        ).fetchone()
        return None if row is None else _profile_from_row(row)

    def list(self) -> list[ConnectionProfile]:
        """按收藏、分组和显示名称的稳定顺序列出所有连接。"""

        rows = self._database.execute(
            _PROFILE_SELECT
            + " ORDER BY favorite DESC, group_name IS NULL, group_name, display_name, connection_id"
        ).fetchall()
        return [_profile_from_row(row) for row in rows]

    def delete(self, connection_id: UUID) -> bool:
        """删除未被 ProxyJump 引用的连接，并返回是否实际删除。"""

        try:
            cursor = self._database.execute(
                "DELETE FROM connection_profiles WHERE connection_id = ?",
                (str(connection_id),),
            )
        except sqlite3.IntegrityError as exc:
            raise ConnectionRepositoryError(
                "CONNECTION_IN_USE", "connection is referenced as a proxy jump"
            ) from exc
        return cursor.rowcount == 1

    def trust_first_host_key(self, candidate: HostKeyCandidate) -> HostKeyRecord:
        """为尚未建立信任的连接持久化首个活动 Host Key。"""

        self._validate_candidate_endpoint(candidate)
        if self.active_host_key(candidate.connection_id) is not None:
            raise ConnectionRepositoryError(
                "HOST_KEY_ALREADY_TRUSTED", "connection already has an active host key"
            )
        record = _record_from_candidate(candidate)
        self._insert_host_key(record)
        return record

    def replace_host_key(
        self,
        candidate: HostKeyCandidate,
        expected_old_fingerprint: str,
    ) -> HostKeyRecord:
        """在单一事务内校验旧指纹、停用旧记录并插入替代记录。"""

        self._validate_candidate_endpoint(candidate)
        connection = self._database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            active = self.active_host_key(candidate.connection_id)
            if (
                active is None
                or active.fingerprint_sha256 != expected_old_fingerprint
            ):
                raise ConnectionRepositoryError(
                    "HOST_KEY_REPLACE_CONFLICT", "active host key changed"
                )
            replaced_at = _utc_now()
            connection.execute(
                """
                UPDATE host_keys
                SET status = 'replaced', replaced_at = ?
                WHERE host_key_id = ? AND status = 'active'
                """,
                (replaced_at, str(active.host_key_id)),
            )
            replacement = _record_from_candidate(candidate)
            self._insert_host_key(replacement)
            connection.execute("COMMIT")
            return replacement
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def active_host_key(self, connection_id: UUID) -> HostKeyRecord | None:
        """返回连接当前生效的 Host Key；尚未信任时返回 None。"""

        row = self._database.execute(
            """
            SELECT host_key_id, connection_id, key_algorithm,
                   fingerprint_sha256, public_key_openssh, status,
                   confirmed_at, replaced_at
            FROM host_keys
            WHERE connection_id = ? AND status = 'active'
            """,
            (str(connection_id),),
        ).fetchone()
        return None if row is None else _host_key_from_row(row)

    def _validate_proxy(
        self, proxy_jump_id: UUID | None, connection_id: UUID
    ) -> None:
        """禁止自引用、不存在的跳板以及多层 ProxyJump 链。"""

        if proxy_jump_id is None:
            return
        if proxy_jump_id == connection_id:
            raise ConnectionRepositoryError(
                "PROXY_JUMP_SELF_REFERENCE", "connection cannot proxy through itself"
            )
        proxy = self.get(proxy_jump_id)
        if proxy is None:
            raise ConnectionRepositoryError(
                "PROXY_JUMP_NOT_FOUND", "proxy jump profile was not found"
            )
        if proxy.proxy_jump_id is not None:
            raise ConnectionRepositoryError(
                "MULTI_HOP_PROXY_FORBIDDEN", "proxy jump cannot contain another jump"
            )

    def _validate_candidate_endpoint(self, candidate: HostKeyCandidate) -> None:
        """确保 Host Key 候选值来自目标连接声明的同一端点。"""

        profile = self.get(candidate.connection_id)
        if profile is None:
            raise ConnectionRepositoryError(
                "CONNECTION_NOT_FOUND", "connection profile was not found"
            )
        if profile.host != candidate.host or profile.port != candidate.port:
            raise ConnectionRepositoryError(
                "HOST_KEY_ENDPOINT_MISMATCH", "host key endpoint does not match profile"
            )

    def _insert_host_key(self, record: HostKeyRecord) -> None:
        """插入 Host Key 记录，并将约束冲突转换为稳定业务错误。"""

        try:
            self._database.execute(
                """
                INSERT INTO host_keys(
                    host_key_id, connection_id, key_algorithm,
                    fingerprint_sha256, public_key_openssh, status,
                    confirmed_at, replaced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.host_key_id),
                    str(record.connection_id),
                    record.key_algorithm,
                    record.fingerprint_sha256,
                    base64.b64decode(record.public_key_openssh_b64, validate=True),
                    record.status,
                    _format_time(record.confirmed_at),
                    None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConnectionRepositoryError(
                "HOST_KEY_CONFLICT", "host key could not be persisted"
            ) from exc


_PROFILE_SELECT = """
SELECT connection_id, display_name, group_name, host, port, username,
       auth_kind, credential_id, passphrase_credential_id, proxy_jump_id,
       favorite, created_at, updated_at, version
FROM connection_profiles
"""


def _profile_parameters(
    connection_id: UUID,
    value: ConnectionProfileInput,
    created_at: str,
    updated_at: str,
) -> tuple:
    return (
        str(connection_id),
        value.display_name,
        value.group_name,
        value.host,
        value.port,
        value.username,
        value.auth_kind,
        str(value.credential_id),
        _uuid_text(value.passphrase_credential_id),
        _uuid_text(value.proxy_jump_id),
        int(value.favorite),
        created_at,
        updated_at,
        1,
    )


def _profile_from_row(row: tuple) -> ConnectionProfile:
    return ConnectionProfile(
        connection_id=UUID(row[0]),
        display_name=row[1],
        group_name=row[2],
        host=row[3],
        port=row[4],
        username=row[5],
        auth_kind=row[6],
        credential_id=UUID(row[7]),
        passphrase_credential_id=_optional_uuid(row[8]),
        proxy_jump_id=_optional_uuid(row[9]),
        favorite=bool(row[10]),
        created_at=_parse_time(row[11]),
        updated_at=_parse_time(row[12]),
        version=row[13],
    )


def _record_from_candidate(candidate: HostKeyCandidate) -> HostKeyRecord:
    return HostKeyRecord(
        host_key_id=uuid4(),
        connection_id=candidate.connection_id,
        key_algorithm=candidate.key_algorithm,
        fingerprint_sha256=candidate.fingerprint_sha256,
        public_key_openssh_b64=candidate.public_key_openssh_b64,
        status="active",
        confirmed_at=datetime.now(timezone.utc),
        replaced_at=None,
    )


def _host_key_from_row(row: tuple) -> HostKeyRecord:
    return HostKeyRecord(
        host_key_id=UUID(row[0]),
        connection_id=UUID(row[1]),
        key_algorithm=row[2],
        fingerprint_sha256=row[3],
        public_key_openssh_b64=base64.b64encode(row[4]).decode("ascii"),
        status=row[5],
        confirmed_at=_parse_time(row[6]),
        replaced_at=None if row[7] is None else _parse_time(row[7]),
    )


def _uuid_text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _optional_uuid(value: str | None) -> UUID | None:
    return None if value is None else UUID(value)


def _utc_now() -> str:
    return _format_time(datetime.now(timezone.utc))


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
