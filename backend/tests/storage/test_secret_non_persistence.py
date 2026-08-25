from pathlib import Path

from harness_shell_sidecar.storage import (
    EncryptedRecord,
    EncryptedRecordStore,
    RuntimeDatabase,
)


PLAINTEXT_MARKER = b"M1-PLAINTEXT-SECRET-6f047bd2"


def test_plaintext_never_reaches_sqlite_files(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    database = RuntimeDatabase.open(path)
    store = EncryptedRecordStore(database, b"d" * 32)
    store.put(EncryptedRecord("secret-test", "marker", 1, PLAINTEXT_MARKER))
    database.close()

    persisted = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )

    assert PLAINTEXT_MARKER not in persisted

