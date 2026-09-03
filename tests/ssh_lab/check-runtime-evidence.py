from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


REQUIRED_SCHEMA_TABLES = (
    "schema_migrations",
    "runtime_records",
    "connection_profiles",
    "host_keys",
    "model_api_configs",
    "agent_conversations",
    "agent_runs",
    "agent_messages",
)
M2_REQUIRED_ROWS = ("connection_profiles", "host_keys")


def main() -> None:
    """Check aggregate plaintext schema-v6 evidence for one local gate."""

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

    schema_present: set[str] = set()
    versions: set[int] = set()
    row_counts = {table: 0 for table in M2_REQUIRED_ROWS}
    manual_sftp_operations = 0
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
            schema_present.update(present)
            if "schema_migrations" in present:
                versions.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                )
            for table in M2_REQUIRED_ROWS:
                if table in present:
                    row_counts[table] += connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
            if "runtime_records" in present:
                manual_sftp_operations += connection.execute(
                    "SELECT COUNT(*) FROM runtime_records "
                    "WHERE record_type = 'manual_sftp_operation'"
                ).fetchone()[0]
        finally:
            connection.close()

    missing_schema = [
        table for table in REQUIRED_SCHEMA_TABLES if table not in schema_present
    ]
    if missing_schema:
        raise SystemExit(
            "required evidence schema is missing: " + ", ".join(missing_schema)
        )
    if versions != {6}:
        raise SystemExit(f"required schema version 6 is missing: {sorted(versions)!r}")
    if manual_sftp:
        missing_rows = []
        if manual_sftp_operations == 0:
            missing_rows.append("manual_sftp_operation")
    else:
        missing_rows = [
            table for table, count in row_counts.items() if count == 0
        ]
    if missing_rows:
        raise SystemExit(
            "required evidence rows are missing: " + ", ".join(missing_rows)
        )


if __name__ == "__main__":
    main()
