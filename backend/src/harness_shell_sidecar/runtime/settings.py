"""单个 Python Runtime 的不可变文件系统配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """从可信绝对根目录派生 Runtime 拥有的全部本地路径。"""

    #: 当前安装的 Runtime 独占的当前用户目录。
    data_dir: Path
    #: 由启动 Alembic 迁移管理的 SQLite 数据库绝对路径。
    database_path: Path
    #: 包含 Python 拥有的诊断日志的目录。
    log_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> RuntimeSettings:
        """派生不可变子路径，不查询环境变量。"""

        if not data_dir.is_absolute():
            raise ValueError("runtime data directory must be absolute")
        return cls(
            data_dir=data_dir,
            database_path=data_dir / "runtime.sqlite3",
            log_dir=data_dir / "logs",
        )
