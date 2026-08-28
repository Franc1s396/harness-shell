"""Encrypted record persistence over runtime SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .crypto import decrypt_payload, encrypt_payload, record_aad, require_data_key
from .database import RuntimeDatabase


@dataclass(frozen=True, slots=True)
class EncryptedRecord:
    """提交给认证加密仓储的不可变明文记录。"""

    #: 区分不同业务记录命名空间的类型名称。
    record_type: str
    #: 在同一 record_type 内唯一的记录标识符。
    record_id: str
    #: 参与 AAD、用于解释明文负载的 Schema 版本。
    schema_version: int
    #: 仅在内存中存在并等待加密或刚完成解密的记录正文。
    payload: bytes

    def __post_init__(self) -> None:
        """拒绝无法形成稳定 AAD 的空身份或非正 Schema 版本。"""

        if not self.record_type or not self.record_id:
            raise ValueError("record type and id must not be empty")
        if self.schema_version <= 0:
            raise ValueError("record schema version must be positive")


class EncryptedRecordStore:
    """使用 AES-GCM 与记录身份 AAD 管理 SQLite 认证加密记录。"""

    def __init__(
        self, database: RuntimeDatabase, data_key: bytes | bytearray
    ) -> None:
        """校验并以可清零 bytearray 持有 256-bit 数据加密 Key。"""

        require_data_key(data_key)
        self._database = database  # 存储 nonce、ciphertext 与记录身份的数据库。
        # Sidecar 运行期间持有并在关闭时原地覆盖的数据加密 Key。
        self._data_key = (
            data_key if isinstance(data_key, bytearray) else bytearray(data_key)
        )

    @property
    def connection(self):
        """暴露共享连接，供需要跨元数据与密文事务的仓储使用。"""

        return self._database.connection

    def put(self, record: EncryptedRecord) -> None:
        """用新随机 nonce 加密并插入或原子更新一条记录。"""

        nonce, ciphertext = encrypt_payload(
            self._data_key,
            record.payload,
            record_aad(
                record.record_type, record.record_id, record.schema_version
            ),
        )
        now = _utc_now()
        self._database.execute(
            """
            INSERT INTO encrypted_records(
                record_type, record_id, schema_version, nonce, ciphertext,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_type, record_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                nonce = excluded.nonce,
                ciphertext = excluded.ciphertext,
                updated_at = excluded.updated_at
            """,
            (
                record.record_type,
                record.record_id,
                record.schema_version,
                nonce,
                ciphertext,
                now,
                now,
            ),
        )

    def get(self, record_type: str, record_id: str) -> EncryptedRecord | None:
        """读取并认证解密记录；不存在时返回 None。"""

        row = self._database.execute(
            """
            SELECT schema_version, nonce, ciphertext
            FROM encrypted_records
            WHERE record_type = ? AND record_id = ?
            """,
            (record_type, record_id),
        ).fetchone()
        if row is None:
            return None
        schema_version, nonce, ciphertext = row
        payload = decrypt_payload(
            self._data_key,
            nonce,
            ciphertext,
            record_aad(record_type, record_id, schema_version),
        )
        return EncryptedRecord(record_type, record_id, schema_version, payload)

    def delete(self, record_type: str, record_id: str) -> bool:
        """按复合身份删除记录，并返回是否实际删除。"""

        cursor = self._database.execute(
            "DELETE FROM encrypted_records WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        )
        return cursor.rowcount == 1

    def zeroize(self) -> None:
        """原地覆盖内存中的数据加密 Key。"""

        for index in range(len(self._data_key)):
            self._data_key[index] = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
