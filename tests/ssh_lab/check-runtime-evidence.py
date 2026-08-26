from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


REQUIRED_TABLES = (
    "audit_entries",
    "trace_spans",
    "artifact_metadata",
    "encrypted_records",
    "vault_meta",
    "vault_secrets",
    "vault_keys",
)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check-runtime-evidence.py EVIDENCE_ROOT")

    root = Path(sys.argv[1])
    if not root.is_dir():
        raise SystemExit(f"evidence root is not a directory: {root}")

    counts = {table: 0 for table in REQUIRED_TABLES}
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
            for table in REQUIRED_TABLES:
                if table in present:
                    counts[table] += connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
        finally:
            connection.close()

    missing = [table for table, count in counts.items() if count == 0]
    if missing:
        raise SystemExit("required evidence rows are missing: " + ", ".join(missing))


if __name__ == "__main__":
    main()
