"""保存非秘密模型 API 配置的 SQLite 仓库。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import UUID, uuid4

from harness_shell_sidecar.storage import RuntimeDatabase

from .contracts import ApiType, ModelApiConfig, ModelApiConfigInput


class ApiConfigRepositoryError(RuntimeError):
    """暴露稳定的 Agent 配置持久化错误码。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存安全错误码和有界诊断消息。"""

        super().__init__(message)
        self.error_code = error_code
        self.safe_message = message


class ApiConfigRepository:
    """持久化 API 元数据，凭据仍保存在 Python 拥有的记录中。"""

    _database: RuntimeDatabase

    def __init__(self, database: RuntimeDatabase) -> None:
        """在本仓库存续期间借用运行时拥有的数据库。"""

        self._database = database

    def create(self, value: ModelApiConfigInput) -> ModelApiConfig:
        """插入配置并返回其持久化表示。"""

        api_config_id = uuid4()
        now = _utc_now()
        self._database.execute(
            """
            INSERT INTO model_api_configs(
                api_config_id, display_name, api_type, base_url, model,
                api_key_credential_id, enabled, created_at, updated_at,
                context_window_size, context_compaction_threshold_ratio, max_output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _config_parameters(api_config_id, value, now, now),
        )
        created = self.get(api_config_id)
        if created is None:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_PERSISTENCE_FAILED",
                "created model API configuration was not found",
            )
        return created

    def get(self, api_config_id: UUID) -> ModelApiConfig | None:
        """按不透明标识返回配置；不存在时返回 None。"""

        row = self._database.execute(
            _CONFIG_SELECT + " WHERE api_config_id = ?",
            (str(api_config_id),),
        ).fetchone()
        return None if row is None else _config_from_row(row)

    def list(self) -> list[ModelApiConfig]:
        """按显示名称和标识的稳定顺序返回配置。"""

        rows = self._database.execute(
            _CONFIG_SELECT + " ORDER BY display_name, api_config_id"
        ).fetchall()
        return [_config_from_row(row) for row in rows]

    def update(
        self,
        api_config_id: UUID,
        value: ModelApiConfigInput,
    ) -> ModelApiConfig:
        """替换全部可变元数据，保留标识和创建时间。"""

        # 1. 先确认配置存在，再按原标识更新可变字段。
        if self.get(api_config_id) is None:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        # 2. 写入新的 Provider 元数据，保留标识与创建时间。
        cursor = self._database.execute(
            """
            UPDATE model_api_configs SET
                display_name = ?, api_type = ?, base_url = ?, model = ?,
                api_key_credential_id = ?, enabled = ?, updated_at = ?,
                context_window_size = ?, context_compaction_threshold_ratio = ?, max_output_tokens = ?
            WHERE api_config_id = ?
            """,
            (
                value.display_name,
                value.api_type.value,
                value.base_url,
                value.model,
                str(value.api_key_credential_id),
                int(value.enabled),
                _utc_now(),
                value.context_window_size,
                value.context_compaction_threshold_ratio,
                value.max_output_tokens,
                str(api_config_id),
            ),
        )
        # 3. 检查写入数量并重新读取，缺失记录不能伪装为更新成功。
        if cursor.rowcount != 1:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_PERSISTENCE_FAILED",
                "model API configuration changed during update",
            )
        updated = self.get(api_config_id)
        if updated is None:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_PERSISTENCE_FAILED",
                "updated model API configuration was not found",
            )
        return updated

    def delete(self, api_config_id: UUID) -> bool:
        """删除未被引用的元数据，不删除凭据记录。"""

        try:
            cursor = self._database.execute(
                "DELETE FROM model_api_configs WHERE api_config_id = ?",
                (str(api_config_id),),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_IN_USE",
                "model API configuration is referenced by an Agent run",
            ) from exc
        return cursor.rowcount == 1


_CONFIG_SELECT = """
SELECT api_config_id, display_name, api_type, base_url, model,
       api_key_credential_id, enabled, created_at, updated_at,
                context_window_size, context_compaction_threshold_ratio, max_output_tokens
FROM model_api_configs
"""


def _config_parameters(
    api_config_id: UUID,
    value: ModelApiConfigInput,
    created_at: str,
    updated_at: str,
) -> tuple[object, ...]:
    """按 SQLite 列的精确顺序转换已校验配置。"""

    return (
        str(api_config_id),
        value.display_name,
        value.api_type.value,
        value.base_url,
        value.model,
        str(value.api_key_credential_id),
        int(value.enabled),
        created_at,
        updated_at,
        value.context_window_size,
        value.context_compaction_threshold_ratio,
        value.max_output_tokens,
    )


def _config_from_row(row: tuple[object, ...]) -> ModelApiConfig:
    """从可信的 schema v7 数据行还原严格配置。"""

    return ModelApiConfig(
        api_config_id=UUID(str(row[0])),
        display_name=str(row[1]),
        api_type=ApiType(str(row[2])),
        base_url=str(row[3]),
        model=str(row[4]),
        api_key_credential_id=UUID(str(row[5])),
        enabled=bool(row[6]),
        created_at=_parse_time(str(row[7])),
        updated_at=_parse_time(str(row[8])),
        context_window_size=row[9],
        context_compaction_threshold_ratio=row[10],
        max_output_tokens=row[11],
    )


def _utc_now() -> str:
    """返回可排序、精确到微秒的 UTC 时间戳。"""

    return _format_time(datetime.now(timezone.utc))


def _format_time(value: datetime) -> str:
    """将带时区的时间格式化为标准 UTC 文本。"""

    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    """将 SQLite 中的标准 UTC 时间戳解析为带时区的 datetime。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
