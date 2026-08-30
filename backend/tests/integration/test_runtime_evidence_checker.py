"""Behavior tests for schema-aware SSH Lab evidence validation."""

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
    "audit_entries",
    "trace_spans",
    "artifact_metadata",
    "encrypted_records",
    "vault_meta",
    "vault_secrets",
    "vault_keys",
)


def create_database(root: Path, *, omit: str | None = None) -> Path:
    """Create a minimal SQLite evidence database with explicit schema objects."""

    path = root / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    try:
        for table in SCHEMA_TABLES:
            if table != omit:
                connection.execute(f"CREATE TABLE {table}(value TEXT)")
        for table in (
            "audit_entries",
            "trace_spans",
            "vault_meta",
            "vault_secrets",
            "vault_keys",
        ):
            connection.execute(f"INSERT INTO {table}(value) VALUES ('evidence')")
        connection.commit()
    finally:
        connection.close()
    return path


def run_checker(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the repository checker exactly as the PowerShell gates do."""

    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_m2_requires_all_schema_but_not_manual_sftp_rows(tmp_path: Path) -> None:
    create_database(tmp_path)

    result = run_checker(tmp_path)

    assert result.returncode == 0, result.stderr


def test_schema_names_inside_row_bytes_do_not_replace_sqlite_schema(tmp_path: Path) -> None:
    path = create_database(tmp_path, omit="artifact_metadata")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO trace_spans(value) VALUES ('artifact_metadata')"
        )
        connection.commit()
    finally:
        connection.close()

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert "artifact_metadata" in result.stderr


def test_manual_sftp_gate_requires_an_encrypted_operation_row(tmp_path: Path) -> None:
    path = create_database(tmp_path)

    missing = run_checker(tmp_path, "--manual-sftp")
    assert missing.returncode != 0
    assert "encrypted_records" in missing.stderr

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO encrypted_records(value) VALUES ('encrypted evidence')"
        )
        connection.commit()
    finally:
        connection.close()

    assert run_checker(tmp_path, "--manual-sftp").returncode == 0


def test_manual_sftp_gate_does_not_require_the_separate_vault_schema(
    tmp_path: Path,
) -> None:
    """The focused SFTP lab owns runtime storage evidence, not Rust Vault evidence."""

    path = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    try:
        for table in (
            "audit_entries",
            "trace_spans",
            "artifact_metadata",
            "encrypted_records",
        ):
            connection.execute(f"CREATE TABLE {table}(value TEXT)")
        for table in ("audit_entries", "trace_spans", "encrypted_records"):
            connection.execute(f"INSERT INTO {table}(value) VALUES ('evidence')")
        connection.commit()
    finally:
        connection.close()

    result = run_checker(tmp_path, "--manual-sftp")

    assert result.returncode == 0, result.stderr
