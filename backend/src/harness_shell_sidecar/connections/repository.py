"""M2 连接配置与 Host Key 的 SQLite 仓库。"""

from __future__ import annotations

import base64
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from uuid import UUID, uuid4

from harness_shell_sidecar.storage.orm import ConnectionProfileRow, HostKeyRow

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

    def __init__(self, session: Session) -> None:
        """借用应用操作的 Session，不拥有提交或关闭权限。"""
        self._session = session  # 当前操作内的数据库上下文。

    def create(self, value: ConnectionProfileInput) -> ConnectionProfile:
        """校验跳板后写入完整配置，提交权归应用层。"""
        connection_id = uuid4()
        now = _utc_now()
        self._validate_proxy(value.proxy_jump_id, connection_id)
        row = ConnectionProfileRow(**dict(zip(_PROFILE_FIELDS, _profile_parameters(connection_id, value, now, now), strict=True)))
        self._session.add(row)
        self._session.flush()
        return _profile_from_row(tuple(getattr(row, field) for field in _PROFILE_FIELDS))

    def update(self, connection_id: UUID, value: ConnectionProfileInput) -> ConnectionProfile:
        """保持单调版本，以当前版本条件更新并拒绝溢出。"""
        current = self.get(connection_id)
        if current is None:
            raise ConnectionRepositoryError("CONNECTION_NOT_FOUND", "connection profile was not found")
        if current.version == 2**53 - 1:
            raise ConnectionRepositoryError("CONNECTION_VERSION_EXHAUSTED", "connection profile version is exhausted")
        self._validate_proxy(value.proxy_jump_id, connection_id)
        values = dict(zip(_PROFILE_FIELDS, _profile_parameters(connection_id, value, _format_time(current.created_at), _utc_now()), strict=True))
        values.pop("connection_id")
        values.pop("created_at")
        values["version"] = current.version + 1
        result = self._session.execute(update(ConnectionProfileRow).where(
            ConnectionProfileRow.connection_id == str(connection_id),
            ConnectionProfileRow.version == current.version).values(**values))
        if result.rowcount != 1:
            raise ConnectionRepositoryError("CONNECTION_PERSISTENCE_FAILED", "connection profile version changed during update")
        updated = self.get(connection_id)
        if updated is None or updated.version != current.version + 1:
            raise ConnectionRepositoryError("CONNECTION_PERSISTENCE_FAILED", "updated profile was not found")
        return updated

    def get(self, connection_id: UUID) -> ConnectionProfile | None:
        """物化最新配置，避免 Session identity map 隐藏版本变更。"""
        row = self._session.execute(_PROFILE_SELECT.where(ConnectionProfileRow.connection_id == str(connection_id))).first()
        return None if row is None else _profile_from_row(row)

    def list(self) -> list[ConnectionProfile]:
        """按现有收藏、分组与名称顺序返回领域模型。"""
        rows = self._session.execute(_PROFILE_SELECT.order_by(
            ConnectionProfileRow.favorite.desc(), ConnectionProfileRow.group_name.is_(None),
            ConnectionProfileRow.group_name, ConnectionProfileRow.display_name, ConnectionProfileRow.connection_id))
        return [_profile_from_row(row) for row in rows]

    def delete(self, connection_id: UUID) -> bool:
        """删除未被跳板引用的配置，约束失败由外层回滚。"""
        try:
            result = self._session.execute(delete(ConnectionProfileRow).where(ConnectionProfileRow.connection_id == str(connection_id)))
        except IntegrityError as error:
            raise ConnectionRepositoryError("CONNECTION_IN_USE", "connection is referenced as a proxy jump") from error
        return result.rowcount == 1

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

    def replace_host_key(self, candidate: HostKeyCandidate, expected_old_fingerprint: str) -> HostKeyRecord:
        """在调用者写事务内先停用旧键，再插入新键。"""
        self._validate_candidate_endpoint(candidate)
        active = self.active_host_key(candidate.connection_id)
        if active is None or active.fingerprint_sha256 != expected_old_fingerprint:
            raise ConnectionRepositoryError("HOST_KEY_REPLACE_CONFLICT", "active host key changed")
        self._session.execute(update(HostKeyRow).where(HostKeyRow.host_key_id == str(active.host_key_id),
            HostKeyRow.status == "active").values(status="replaced", replaced_at=_utc_now()))
        replacement = _record_from_candidate(candidate)
        self._insert_host_key(replacement)
        return replacement

    def active_host_key(self, connection_id: UUID) -> HostKeyRecord | None:
        """读取当前唯一活动 Host Key 并返回领域快照。"""
        row = self._session.execute(select(*[getattr(HostKeyRow, name) for name in _HOST_KEY_FIELDS]).where(
            HostKeyRow.connection_id == str(connection_id), HostKeyRow.status == "active")).first()
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
        """立即 flush 使唯一键冲突在仓库边界显式映射。"""
        try:
            self._session.add(HostKeyRow(host_key_id=str(record.host_key_id), connection_id=str(record.connection_id),
                key_algorithm=record.key_algorithm, fingerprint_sha256=record.fingerprint_sha256,
                public_key_openssh=base64.b64decode(record.public_key_openssh_b64, validate=True),
                status=record.status, confirmed_at=_format_time(record.confirmed_at), replaced_at=None))
            self._session.flush()
        except IntegrityError as error:
            raise ConnectionRepositoryError("HOST_KEY_CONFLICT", "host key could not be persisted") from error


_PROFILE_FIELDS = ('connection_id', 'display_name', 'group_name', 'host', 'port', 'username', 'auth_kind', 'credential_id', 'passphrase_credential_id', 'proxy_jump_id', 'favorite', 'created_at', 'updated_at', 'version')
_HOST_KEY_FIELDS = ('host_key_id', 'connection_id', 'key_algorithm', 'fingerprint_sha256', 'public_key_openssh', 'status', 'confirmed_at', 'replaced_at')
_PROFILE_SELECT = select(*[getattr(ConnectionProfileRow, name) for name in _PROFILE_FIELDS])


def _profile_parameters(
    connection_id: UUID,
    value: ConnectionProfileInput,
    created_at: str,
    updated_at: str,
) -> tuple:
    """将严格输入转换为具名 ORM 字段的对应存储值。"""
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
    """将查询行物化为不可变连接配置，不泄露 ORM 对象。"""
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
    """为已验证候选生成首个活动 Host Key 领域快照。"""
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
    """将存储字节与时间转换为对外 Host Key 快照。"""
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
    """保留空引用并将非空 UUID 转为存储文本。"""
    return None if value is None else str(value)


def _optional_uuid(value: str | None) -> UUID | None:
    """将可空存储引用还原为 UUID。"""
    return None if value is None else UUID(value)


def _utc_now() -> str:
    """生成微秒精度的 UTC 存储时间。"""
    return _format_time(datetime.now(timezone.utc))


def _format_time(value: datetime) -> str:
    """统一以 UTC 微秒文本保存时间。"""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    """从规范存储文本还原带时区时间。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
