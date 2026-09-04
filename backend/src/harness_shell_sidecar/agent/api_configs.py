"""SQLite repository for non-secret model API configurations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import UUID, uuid4

from harness_shell_sidecar.storage import RuntimeDatabase

from .contracts import ApiType, ModelApiConfig, ModelApiConfigInput


class ApiConfigRepositoryError(RuntimeError):
    """Expose one stable Agent configuration persistence error code."""

    def __init__(self, error_code: str, message: str) -> None:
        """Store a safe error code and bounded diagnostic message."""

        super().__init__(message)
        self.error_code = error_code
        self.safe_message = message


class ApiConfigRepository:
    """Persist API metadata while credentials remain in Python-owned records."""

    _database: RuntimeDatabase

    def __init__(self, database: RuntimeDatabase) -> None:
        """Borrow the runtime-owned database for this repository's lifetime."""

        self._database = database

    def create(self, value: ModelApiConfigInput) -> ModelApiConfig:
        """Insert one configuration and return its persisted representation."""

        api_config_id = uuid4()
        now = _utc_now()
        self._database.execute(
            """
            INSERT INTO model_api_configs(
                api_config_id, display_name, api_type, base_url, model,
                api_key_credential_id, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        """Return one configuration by opaque identity or None when absent."""

        row = self._database.execute(
            _CONFIG_SELECT + " WHERE api_config_id = ?",
            (str(api_config_id),),
        ).fetchone()
        return None if row is None else _config_from_row(row)

    def list(self) -> list[ModelApiConfig]:
        """Return configurations in stable display-name and identity order."""

        rows = self._database.execute(
            _CONFIG_SELECT + " ORDER BY display_name, api_config_id"
        ).fetchall()
        return [_config_from_row(row) for row in rows]

    def update(
        self,
        api_config_id: UUID,
        value: ModelApiConfigInput,
    ) -> ModelApiConfig:
        """Replace all mutable metadata while preserving identity and creation time."""

        if self.get(api_config_id) is None:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        cursor = self._database.execute(
            """
            UPDATE model_api_configs SET
                display_name = ?, api_type = ?, base_url = ?, model = ?,
                api_key_credential_id = ?, enabled = ?, updated_at = ?
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
                str(api_config_id),
            ),
        )
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
        """Delete unreferenced metadata without deleting the credential record."""

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
       api_key_credential_id, enabled, created_at, updated_at
FROM model_api_configs
"""


def _config_parameters(
    api_config_id: UUID,
    value: ModelApiConfigInput,
    created_at: str,
    updated_at: str,
) -> tuple[object, ...]:
    """Convert a validated configuration into the exact SQLite row order."""

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
    )


def _config_from_row(row: tuple[object, ...]) -> ModelApiConfig:
    """Restore one strict configuration from a trusted schema-v4 row."""

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
    )


def _utc_now() -> str:
    """Return a sortable UTC timestamp with microsecond precision."""

    return _format_time(datetime.now(timezone.utc))


def _format_time(value: datetime) -> str:
    """Format one aware timestamp as canonical UTC text."""

    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    """Parse one canonical UTC SQLite timestamp into an aware datetime."""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
