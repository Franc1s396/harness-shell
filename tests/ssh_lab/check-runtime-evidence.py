from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


REQUIRED_SCHEMA_TABLES = (
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
M2_REQUIRED_ROWS = ("connection_profiles", "host_keys")


def main() -> None:
    """检查本地门禁的聚合明文 Alembic 基线 证据。"""

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
    versions: set[str] = set()
    row_counts = {table: 0 for table in M2_REQUIRED_ROWS}
    manual_sftp_operations = 0
    database_paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".db", ".sqlite", ".sqlite3"}
    )
    for path in database_paths:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            present = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            forbidden = present & {"schema_migrations", "audit_entries", "trace_spans", "artifact_metadata"}
            if forbidden:
                raise SystemExit(f"unexpected evidence tables: {sorted(forbidden)}")
            strict = {row[1]: row[5] for row in connection.execute("PRAGMA table_list")}
            for table in REQUIRED_SCHEMA_TABLES[1:]:
                if table in present and strict[table] != 1:
                    raise SystemExit(f"evidence table must be STRICT: {table}")
            schema_present.update(present)
            if "alembic_version" in present:
                versions.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchall()
                )
            if "alembic_version" in present:
                actual = connection.execute("SELECT version_num FROM alembic_version").fetchall()
                if actual != [("0001_initial",)]:
                    raise SystemExit("required Alembic revision 0001_initial is missing")
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
    if versions != {"0001_initial"}:
        raise SystemExit(f"required Alembic revision 0001_initial is missing: {sorted(versions)!r}")
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
