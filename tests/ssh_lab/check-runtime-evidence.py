from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


M2_REQUIRED_SCHEMA_TABLES = (
    "audit_entries",
    "trace_spans",
    "artifact_metadata",
    "encrypted_records",
    "vault_meta",
    "vault_secrets",
    "vault_keys",
)
MANUAL_SFTP_REQUIRED_SCHEMA_TABLES = (
    "audit_entries",
    "trace_spans",
    "artifact_metadata",
    "encrypted_records",
)
M2_REQUIRED_ROW_TABLES = (
    "audit_entries",
    "trace_spans",
    "vault_meta",
    "vault_secrets",
    "vault_keys",
)
MANUAL_SFTP_REQUIRED_ROW_TABLES = (
    "audit_entries",
    "trace_spans",
    "encrypted_records",
)


def main() -> None:
    """Check aggregate evidence rows for the selected local gate."""

    if len(sys.argv) not in {2, 3}:
        raise SystemExit(
            "usage: check-runtime-evidence.py EVIDENCE_ROOT [--manual-sftp]"
        )
    manual_sftp = len(sys.argv) == 3 and sys.argv[2] == "--manual-sftp"
    if len(sys.argv) == 3 and not manual_sftp:
        raise SystemExit("unknown evidence mode: " + sys.argv[2])

    root = Path(sys.argv[1])
    if not root.is_dir():
        raise SystemExit(f"evidence root is not a directory: {root}")

    required_row_tables = (
        MANUAL_SFTP_REQUIRED_ROW_TABLES if manual_sftp else M2_REQUIRED_ROW_TABLES
    )
    required_schema_tables = (
        MANUAL_SFTP_REQUIRED_SCHEMA_TABLES
        if manual_sftp
        else M2_REQUIRED_SCHEMA_TABLES
    )
    counts = {table: 0 for table in required_row_tables}
    present_schema: set[str] = set()
    database_paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".db", ".sqlite", ".sqlite3"}
    )
    for path in database_paths:
        connection = sqlite3.connect(path)
        try:
            present = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            present_schema.update(present)
            for table in required_row_tables:
                if table in present:
                    counts[table] += connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
        finally:
            connection.close()

    missing_schema = [
        table for table in required_schema_tables if table not in present_schema
    ]
    if missing_schema:
        raise SystemExit(
            "required evidence schema is missing: " + ", ".join(missing_schema)
        )
    missing_rows = [table for table, count in counts.items() if count == 0]
    if missing_rows:
        raise SystemExit(
            "required evidence rows are missing: " + ", ".join(missing_rows)
        )


if __name__ == "__main__":
    main()
