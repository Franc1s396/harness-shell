use std::path::PathBuf;

use harness_shell_lib::sftp::{
    journal::{LocalSftpOperationJournal, LocalSftpOperationRecord, OperationKind, OperationState},
    models::{EntryType, TransferSnapshot},
};
use time::OffsetDateTime;
use uuid::Uuid;

fn journal_record(local_path: &str) -> LocalSftpOperationRecord {
    LocalSftpOperationRecord {
        operation_id: Uuid::new_v4(),
        remote_operation_id: None,
        kind: OperationKind::Download,
        state: OperationState::Transferring,
        connection_id: Uuid::new_v4(),
        host_label: Some("demo-host".to_owned()),
        local_path: Some(PathBuf::from(local_path)),
        remote_path: "/home/demo/payload.bin".to_owned(),
        expected_sha256: Some(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
        ),
        target_snapshot: Some(TransferSnapshot {
            path: "/home/demo/payload.bin".to_owned(),
            exists: true,
            entry_type: Some(EntryType::File),
            size: Some(3),
            mtime_ns: Some("1770000000000000000".to_owned()),
            sha256: Some(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
            ),
        }),
        created_at: OffsetDateTime::now_utc(),
    }
}

#[test]
fn journal_round_trips_without_plaintext_path_in_sqlite() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&path).unwrap();
    let marker = r"C:\secret-marker\payload.bin";
    let record = journal_record(marker);

    journal.put(&record).unwrap();
    assert_eq!(journal.get(record.operation_id).unwrap(), Some(record));
    drop(journal);

    let persisted = std::fs::read(path).unwrap();
    assert!(!persisted
        .windows(marker.len())
        .any(|window| window == marker.as_bytes()));
}

#[test]
fn journal_rejects_wrong_database_identity_and_malformed_ciphertext() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("manual-sftp.sqlite3");
    {
        let connection = rusqlite::Connection::open(&path).unwrap();
        connection
            .pragma_update(None, "application_id", 42)
            .unwrap();
        connection.pragma_update(None, "user_version", 1).unwrap();
    }
    let error = LocalSftpOperationJournal::open(&path).unwrap_err();
    assert_eq!(error.code(), "SFTP_JOURNAL_INVALID");

    let valid_path = directory.path().join("valid.sqlite3");
    let journal = LocalSftpOperationJournal::open(&valid_path).unwrap();
    let operation_id = Uuid::new_v4();
    journal
        .insert_ciphertext_for_test(operation_id, &[1_u8, 2, 3])
        .unwrap();
    let error = journal.get(operation_id).unwrap_err();
    assert_eq!(error.code(), "SFTP_JOURNAL_DECRYPT_FAILED");
}

#[test]
fn journal_rejects_extra_business_tables() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&path).unwrap();
    drop(journal);
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection
        .execute("CREATE TABLE unexpected(value TEXT) STRICT", [])
        .unwrap();
    drop(connection);

    let error = LocalSftpOperationJournal::open(&path).unwrap_err();
    assert_eq!(error.code(), "SFTP_JOURNAL_INVALID");
}

#[test]
fn journal_keeps_non_terminal_records_until_explicit_delete() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&path).unwrap();
    let record = journal_record(r"C:\stays\part.bin");
    journal.put(&record).unwrap();
    drop(journal);

    let reopened = LocalSftpOperationJournal::open(&path).unwrap();
    assert_eq!(reopened.list_non_terminal().unwrap(), vec![record.clone()]);
    assert!(reopened.delete(record.operation_id).unwrap());
    assert_eq!(reopened.get(record.operation_id).unwrap(), None);
}
