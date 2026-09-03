"""Behavior tests for schema-v6 SSH Lab evidence validation."""

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
    "schema_migrations",
    "runtime_records",
    "connection_profiles",
    "host_keys",
    "model_api_configs",
    "agent_conversations",
    "agent_runs",
    "agent_messages",
)


def create_database(root: Path, *, omit: str | None = None) -> Path:
    """Create one minimal schema-v6 evidence database."""

    path = root / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    try:
        for table in SCHEMA_TABLES:
            if table == omit:
                continue
            if table == "schema_migrations":
                connection.execute("CREATE TABLE schema_migrations(version INTEGER)")
                connection.execute("INSERT INTO schema_migrations VALUES (6)")
            elif table == "runtime_records":
                connection.execute(
                    "CREATE TABLE runtime_records(record_type TEXT, record_id TEXT)"
                )
            else:
                connection.execute(f"CREATE TABLE {table}(value TEXT)")
        for table in ("connection_profiles", "host_keys"):
            connection.execute(f"INSERT INTO {table}(value) VALUES ('evidence')")
        connection.commit()
    finally:
        connection.close()
    return path


def run_checker(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the repository checker exactly as PowerShell gates do."""

    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_m2_requires_complete_schema_v6_and_ssh_runtime_rows(tmp_path: Path) -> None:
    """M2 may leave generic records empty because SSH tests inject credentials."""

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


def test_checker_rejects_non_v6_schema_version(tmp_path: Path) -> None:
    path = create_database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_migrations SET version = 4")
    connection.commit()
    connection.close()
    result = run_checker(tmp_path)
    assert result.returncode != 0
    assert "schema version 6" in result.stderr
