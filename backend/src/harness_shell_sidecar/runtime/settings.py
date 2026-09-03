"""Immutable filesystem settings for one Python Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Derive every Runtime-owned local path from one trusted absolute root."""

    #: Current-user directory exclusively owned by this Runtime installation.
    data_dir: Path
    #: Fresh-only schema-v6 SQLite database path.
    database_path: Path
    #: Directory containing Python-owned diagnostic logs.
    log_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> RuntimeSettings:
        """Derive immutable child paths without environment-variable lookup."""

        if not data_dir.is_absolute():
            raise ValueError("runtime data directory must be absolute")
        return cls(
            data_dir=data_dir,
            database_path=data_dir / "runtime.sqlite3",
            log_dir=data_dir / "logs",
        )
