"""Alembic 基线 SSH Lab 证据校验的行为测试。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "ssh_lab"
    / "check-runtime-evidence.py"
)
SCHEMA_TABLES = (
    "alembic_version",
    "runtime_records",
    "connection_profiles",
    "host_keys",
    "model_api_configs",
    "agent_conversations",
    "agent_runs",
    "agent_messages",
    "agent_context_summaries",
)


def create_database(root: Path, *, omit: str | None = None) -> Path:
    """创建最小 Alembic 基线 证据数据库。"""

    path = root / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    try:
        for table in SCHEMA_TABLES:
            if table == omit:
                continue
            if table == "alembic_version":
                connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
                connection.execute("INSERT INTO alembic_version VALUES ('0001_initial')")
            elif table == "runtime_records":
                connection.execute(
                    "CREATE TABLE runtime_records(record_type TEXT, record_id TEXT) STRICT"
                )
            else:
                connection.execute(f"CREATE TABLE {table}(value TEXT) STRICT")
        for table in ("connection_profiles", "host_keys"):
            connection.execute(f"INSERT INTO {table}(value) VALUES ('evidence')")
        connection.commit()
    finally:
        connection.close()
    return path


def run_checker(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """按 PowerShell 门禁的实际方式运行仓库检查器。"""

    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_m2_requires_complete_alembic_baseline_and_ssh_runtime_rows(tmp_path: Path) -> None:
    """M2 的通用记录可以为空，因为 SSH 测试注入凭据。"""

    create_database(tmp_path)
    result = run_checker(tmp_path)
    assert result.returncode == 0, result.stderr


def test_schema_names_inside_row_bytes_do_not_replace_schema(tmp_path: Path) -> None:
    path = create_database(tmp_path, omit="agent_messages")
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO runtime_records VALUES ('test', 'agent_messages')"
    )
    connection.commit()
    connection.close()
    result = run_checker(tmp_path)
    assert result.returncode != 0
    assert "agent_messages" in result.stderr


def test_manual_sftp_gate_requires_plaintext_operation_record(tmp_path: Path) -> None:
    path = create_database(tmp_path)
    missing = run_checker(tmp_path, "--manual-sftp")
    assert missing.returncode != 0
    assert "manual_sftp_operation" in missing.stderr

    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO runtime_records VALUES ('manual_sftp_operation', 'operation-id')"
    )
    connection.commit()
    connection.close()
    assert run_checker(tmp_path, "--manual-sftp").returncode == 0


def test_checker_rejects_unknown_revision(tmp_path: Path) -> None:
    path = create_database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE alembic_version SET version_num = 'unknown'")
    connection.commit()
    connection.close()
    result = run_checker(tmp_path)
    assert result.returncode != 0
    assert "Alembic revision 0001_initial" in result.stderr
