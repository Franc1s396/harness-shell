"""保存非秘密模型 API 配置的 SQLite 仓库。"""

from __future__ import annotations

from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from uuid import UUID, uuid4

from harness_shell_sidecar.storage.orm import ModelApiConfigRow

from .contracts import ApiType, ModelApiConfig, ModelApiConfigInput


class ApiConfigRepositoryError(RuntimeError):
    """暴露稳定的 Agent 配置持久化错误码。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存安全错误码和有界诊断消息。"""

        super().__init__(message)
        self.error_code = error_code
        self.safe_message = message


class ApiConfigRepository:
    """借用操作 Session 管理 Provider 元数据，不单独提交凭据。"""

    def __init__(self, session: Session) -> None:
        """绑定调用者拥有的短 Session。"""
        self._session = session  # 生命周期限于当前数据库操作。

    def create(self, value: ModelApiConfigInput) -> ModelApiConfig:
        """插入已校验的 Provider 配置并物化结果。"""
        now = _utc_now()
        parameters = _config_parameters(uuid4(), value, now, now)
        self._session.add(ModelApiConfigRow(**dict(zip(_CONFIG_FIELDS, parameters, strict=True))))
        self._session.flush()
        return _config_from_row(parameters)

    def get(self, api_config_id: UUID) -> ModelApiConfig | None:
        """按标识读取当前配置。"""
        row = self._session.execute(_CONFIG_SELECT.where(ModelApiConfigRow.api_config_id == str(api_config_id))).first()
        return None if row is None else _config_from_row(row)

    def list(self) -> list[ModelApiConfig]:
        """按稳定名称和标识顺序返回配置。"""
        return [_config_from_row(row) for row in self._session.execute(
            _CONFIG_SELECT.order_by(ModelApiConfigRow.display_name, ModelApiConfigRow.api_config_id))]

    def update(self, api_config_id: UUID, value: ModelApiConfigInput) -> ModelApiConfig:
        """替换配置同时保留身份及创建时间。"""
        current = self.get(api_config_id)
        if current is None:
            raise ApiConfigRepositoryError("MODEL_API_CONFIG_NOT_FOUND", "model API configuration was not found")
        parameters = _config_parameters(api_config_id, value, _format_time(current.created_at), _utc_now())
        values = dict(zip(_CONFIG_FIELDS, parameters, strict=True))
        values.pop("api_config_id")
        values.pop("created_at")
        result = self._session.execute(update(ModelApiConfigRow).where(
            ModelApiConfigRow.api_config_id == str(api_config_id)).values(**values))
        if result.rowcount != 1:
            raise ApiConfigRepositoryError("MODEL_API_CONFIG_PERSISTENCE_FAILED", "model API configuration changed during update")
        return _config_from_row(parameters)

    def delete(self, api_config_id: UUID) -> bool:
        """删除未被 Run 引用的配置，应用层同时处理其凭据。"""
        try:
            result = self._session.execute(delete(ModelApiConfigRow).where(ModelApiConfigRow.api_config_id == str(api_config_id)))
        except IntegrityError as error:
            raise ApiConfigRepositoryError("MODEL_API_CONFIG_IN_USE", "model API configuration is referenced by an Agent run") from error
        return result.rowcount == 1


_CONFIG_FIELDS = ('api_config_id', 'display_name', 'api_type', 'base_url', 'model', 'api_key_credential_id', 'enabled', 'created_at', 'updated_at', 'context_window_size', 'context_compaction_threshold_ratio', 'max_output_tokens')
_CONFIG_SELECT = select(*[getattr(ModelApiConfigRow, name) for name in _CONFIG_FIELDS])


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
    """从已校验的 ORM 数据行还原严格配置。"""

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
