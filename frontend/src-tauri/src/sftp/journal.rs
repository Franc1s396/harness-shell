#[cfg(debug_assertions)]
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Condvar, Mutex,
};
#[cfg(debug_assertions)]
use std::time::Duration;
use std::{fmt, path::PathBuf, thread};

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use time::OffsetDateTime;
use tokio::sync::{mpsc, oneshot};
use uuid::Uuid;

use crate::vault::dpapi;

use super::models::TransferSnapshot;

const APPLICATION_ID: i64 = 0x4853_4654;
const SCHEMA_VERSION: i64 = 1;
const DPAPI_DESCRIPTION: &str = "Harness Shell manual SFTP operation";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationKind {
    Upload,
    Download,
    Mkdir,
    Rename,
    Remove,
    RecursiveDelete,
    Recovery,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationState {
    Preparing,
    Transferring,
    Verifying,
    Committing,
    Succeeded,
    Failed,
    Cancelled,
    CleanupRequired,
    OutcomeUnknown,
}

impl OperationState {
    fn is_terminal(self) -> bool {
        matches!(self, Self::Succeeded | Self::Failed | Self::Cancelled)
    }
}

/// DPAPI-protected local operation state. The complete record is encrypted as one blob.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LocalSftpOperationRecord {
    pub operation_id: Uuid,
    /// Sidecar operation identity when a retained local recovery action coordinates a distinct
    /// remote mutation. Persisting the association makes restart reconciliation exact.
    #[serde(default)]
    pub remote_operation_id: Option<Uuid>,
    pub kind: OperationKind,
    pub state: OperationState,
    pub connection_id: Uuid,
    /// Safe display label captured from the validated connection context. Older encrypted
    /// records may not contain it, so recovery presentation uses an explicit saved-profile label.
    #[serde(default)]
    pub host_label: Option<String>,
    pub local_path: Option<PathBuf>,
    pub remote_path: String,
    pub expected_sha256: Option<String>,
    pub target_snapshot: Option<TransferSnapshot>,
    pub created_at: OffsetDateTime,
}

pub struct LocalSftpOperationJournal {
    connection: Connection,
}

/// Async handle for the dedicated worker that exclusively owns the SQLite connection.
///
/// DPAPI and SQLite are blocking APIs. Sending a command is a short in-memory operation; the
/// worker thread performs all protected-record and database I/O in strict receive order.
#[derive(Clone)]
pub struct LocalSftpJournalActor {
    commands: mpsc::UnboundedSender<JournalCommand>,
}

/// Deterministic contract-test fault at the terminal-record deletion boundary.
#[doc(hidden)]
#[derive(Clone, Default)]
#[cfg(debug_assertions)]
pub struct JournalFaultTestGate {
    fail_next_put: Arc<AtomicBool>,
    fail_next_delete: Arc<AtomicBool>,
    return_false_next_delete: Arc<AtomicBool>,
    put_block: Arc<JournalPutBlock>,
}

#[derive(Default)]
#[cfg(debug_assertions)]
struct JournalPutBlock {
    state: Mutex<JournalPutBlockState>,
    released: Condvar,
}

#[derive(Default)]
#[cfg(debug_assertions)]
struct JournalPutBlockState {
    armed: bool,
    skip_puts: usize,
    blocked: bool,
    released: bool,
    operation_id: Option<Uuid>,
}

#[cfg(debug_assertions)]
impl JournalFaultTestGate {
    #[doc(hidden)]
    pub fn new() -> Self {
        Self::default()
    }

    #[doc(hidden)]
    pub fn fail_next_delete(&self) {
        self.fail_next_delete.store(true, Ordering::Release);
    }

    #[doc(hidden)]
    pub fn return_false_next_delete(&self) {
        self.return_false_next_delete.store(true, Ordering::Release);
    }

    #[doc(hidden)]
    pub fn fail_next_put(&self) {
        self.fail_next_put.store(true, Ordering::Release);
    }

    fn take_put_failure(&self) -> bool {
        self.fail_next_put.swap(false, Ordering::AcqRel)
    }

    #[doc(hidden)]
    pub fn block_next_put(&self) {
        self.block_put_after(0);
    }

    #[doc(hidden)]
    pub fn block_put_after(&self, skip_puts: usize) {
        let mut state = self
            .put_block
            .state
            .lock()
            .expect("journal test gate poisoned");
        state.armed = true;
        state.skip_puts = skip_puts;
        state.blocked = false;
        state.released = false;
        state.operation_id = None;
    }

    #[doc(hidden)]
    pub async fn wait_until_put_blocked(&self) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while !self
                .put_block
                .state
                .lock()
                .expect("journal test gate poisoned")
                .blocked
            {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("timed out waiting for journal put test gate");
    }

    #[doc(hidden)]
    pub fn blocked_put_operation_id(&self) -> Option<Uuid> {
        self.put_block
            .state
            .lock()
            .expect("journal test gate poisoned")
            .operation_id
    }

    #[doc(hidden)]
    pub fn release_put(&self) {
        let mut state = self
            .put_block
            .state
            .lock()
            .expect("journal test gate poisoned");
        state.released = true;
        self.put_block.released.notify_all();
    }

    fn block_put_if_requested(&self, operation_id: Uuid) {
        let mut state = self
            .put_block
            .state
            .lock()
            .expect("journal test gate poisoned");
        if !state.armed {
            return;
        }
        if state.skip_puts > 0 {
            state.skip_puts -= 1;
            return;
        }
        state.armed = false;
        state.blocked = true;
        state.operation_id = Some(operation_id);
        while !state.released {
            let (next, wait) = self
                .put_block
                .released
                .wait_timeout(state, Duration::from_secs(5))
                .expect("journal test gate poisoned");
            assert!(
                !wait.timed_out(),
                "timed out releasing journal put test gate"
            );
            state = next;
        }
        state.blocked = false;
    }

    fn take_delete_failure(&self) -> bool {
        self.fail_next_delete.swap(false, Ordering::AcqRel)
    }

    fn take_delete_false(&self) -> bool {
        self.return_false_next_delete.swap(false, Ordering::AcqRel)
    }
}

enum JournalCommand {
    Put {
        record: LocalSftpOperationRecord,
        reply: oneshot::Sender<Result<(), JournalError>>,
    },
    Get {
        operation_id: Uuid,
        reply: oneshot::Sender<Result<Option<LocalSftpOperationRecord>, JournalError>>,
    },
    ListNonTerminal {
        reply: oneshot::Sender<Result<Vec<LocalSftpOperationRecord>, JournalError>>,
    },
    Delete {
        operation_id: Uuid,
        reply: oneshot::Sender<Result<bool, JournalError>>,
    },
}

impl fmt::Debug for LocalSftpOperationJournal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LocalSftpOperationJournal")
            .field("connection", &"<redacted>")
            .finish()
    }
}

impl fmt::Debug for LocalSftpJournalActor {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LocalSftpJournalActor")
            .field("commands", &"<dedicated blocking worker>")
            .finish()
    }
}

#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct JournalError {
    code: &'static str,
    message: &'static str,
}

impl JournalError {
    fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    pub fn code(&self) -> &'static str {
        self.code
    }
}

impl LocalSftpJournalActor {
    /// Move the journal into its single blocking owner.
    pub fn spawn(journal: LocalSftpOperationJournal) -> Self {
        #[cfg(debug_assertions)]
        {
            Self::spawn_internal(journal, None)
        }
        #[cfg(not(debug_assertions))]
        {
            Self::spawn_internal(journal)
        }
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn spawn_with_test_fault(
        journal: LocalSftpOperationJournal,
        fault: JournalFaultTestGate,
    ) -> Self {
        Self::spawn_internal(journal, Some(fault))
    }

    fn spawn_internal(
        journal: LocalSftpOperationJournal,
        #[cfg(debug_assertions)] fault: Option<JournalFaultTestGate>,
    ) -> Self {
        let (commands, mut receiver) = mpsc::unbounded_channel();
        thread::Builder::new()
            .name("manual-sftp-journal".to_owned())
            .spawn(move || {
                while let Some(command) = receiver.blocking_recv() {
                    match command {
                        JournalCommand::Put { record, reply } => {
                            #[cfg(debug_assertions)]
                            if let Some(fault) = &fault {
                                fault.block_put_if_requested(record.operation_id);
                            }
                            let result = {
                                #[cfg(debug_assertions)]
                                {
                                    if fault
                                        .as_ref()
                                        .is_some_and(JournalFaultTestGate::take_put_failure)
                                    {
                                        Err(journal_unavailable())
                                    } else {
                                        journal.put(&record)
                                    }
                                }
                                #[cfg(not(debug_assertions))]
                                {
                                    journal.put(&record)
                                }
                            };
                            let _ = reply.send(result);
                        }
                        JournalCommand::Get {
                            operation_id,
                            reply,
                        } => {
                            let _ = reply.send(journal.get(operation_id));
                        }
                        JournalCommand::ListNonTerminal { reply } => {
                            let _ = reply.send(journal.list_non_terminal());
                        }
                        JournalCommand::Delete {
                            operation_id,
                            reply,
                        } => {
                            let result = {
                                #[cfg(debug_assertions)]
                                {
                                    if fault
                                        .as_ref()
                                        .is_some_and(JournalFaultTestGate::take_delete_failure)
                                    {
                                        Err(journal_unavailable())
                                    } else if fault
                                        .as_ref()
                                        .is_some_and(JournalFaultTestGate::take_delete_false)
                                    {
                                        Ok(false)
                                    } else {
                                        journal.delete(operation_id)
                                    }
                                }
                                #[cfg(not(debug_assertions))]
                                {
                                    journal.delete(operation_id)
                                }
                            };
                            let _ = reply.send(result);
                        }
                    }
                }
            })
            .expect("failed to start the manual SFTP journal worker");
        Self { commands }
    }

    pub async fn put(&self, record: LocalSftpOperationRecord) -> Result<(), JournalError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(JournalCommand::Put { record, reply })
            .map_err(|_| journal_unavailable())?;
        response.await.map_err(|_| journal_unavailable())?
    }

    pub async fn get(
        &self,
        operation_id: Uuid,
    ) -> Result<Option<LocalSftpOperationRecord>, JournalError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(JournalCommand::Get {
                operation_id,
                reply,
            })
            .map_err(|_| journal_unavailable())?;
        response.await.map_err(|_| journal_unavailable())?
    }

    pub async fn list_non_terminal(&self) -> Result<Vec<LocalSftpOperationRecord>, JournalError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(JournalCommand::ListNonTerminal { reply })
            .map_err(|_| journal_unavailable())?;
        response.await.map_err(|_| journal_unavailable())?
    }

    pub async fn delete(&self, operation_id: Uuid) -> Result<bool, JournalError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(JournalCommand::Delete {
                operation_id,
                reply,
            })
            .map_err(|_| journal_unavailable())?;
        response.await.map_err(|_| journal_unavailable())?
    }
}

impl LocalSftpOperationJournal {
    pub fn open(path: &std::path::Path) -> Result<Self, JournalError> {
        let connection = Connection::open(path).map_err(|_| journal_unavailable())?;
        connection
            .pragma_update(None, "journal_mode", "DELETE")
            .map_err(|_| journal_unavailable())?;
        connection
            .pragma_update(None, "foreign_keys", true)
            .map_err(|_| journal_unavailable())?;

        let application_id: i64 = connection
            .pragma_query_value(None, "application_id", |row| row.get(0))
            .map_err(|_| journal_invalid())?;
        let user_version: i64 = connection
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .map_err(|_| journal_invalid())?;
        let table_count: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
                [],
                |row| row.get(0),
            )
            .map_err(|_| journal_invalid())?;

        if application_id == 0 && user_version == 0 && table_count == 0 {
            initialize(&connection)?;
        } else if application_id != APPLICATION_ID || user_version != SCHEMA_VERSION {
            return Err(journal_invalid());
        }
        validate_schema(&connection)?;
        Ok(Self { connection })
    }

    pub fn put(&self, record: &LocalSftpOperationRecord) -> Result<(), JournalError> {
        let plaintext = serde_json::to_vec(record).map_err(|_| journal_invalid())?;
        let protected =
            dpapi::protect(&plaintext, DPAPI_DESCRIPTION).map_err(|_| journal_encrypt_failed())?;
        self.connection
            .execute(
                "INSERT INTO sftp_operation_journal(operation_id, protected_record) VALUES (?1, ?2)
                 ON CONFLICT(operation_id) DO UPDATE SET
                     protected_record = excluded.protected_record,
                     updated_at = unixepoch()",
                params![record.operation_id.to_string(), protected],
            )
            .map_err(|_| journal_unavailable())?;
        Ok(())
    }

    pub fn get(
        &self,
        operation_id: Uuid,
    ) -> Result<Option<LocalSftpOperationRecord>, JournalError> {
        let ciphertext: Option<Vec<u8>> = self
            .connection
            .query_row(
                "SELECT protected_record FROM sftp_operation_journal WHERE operation_id = ?1",
                [operation_id.to_string()],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| journal_unavailable())?;
        let Some(ciphertext) = ciphertext else {
            return Ok(None);
        };
        let plaintext = dpapi::unprotect(&ciphertext).map_err(|_| journal_decrypt_failed())?;
        let record: LocalSftpOperationRecord =
            serde_json::from_slice(&plaintext).map_err(|_| journal_decrypt_failed())?;
        if record.operation_id != operation_id {
            return Err(journal_decrypt_failed());
        }
        Ok(Some(record))
    }

    pub fn list_non_terminal(&self) -> Result<Vec<LocalSftpOperationRecord>, JournalError> {
        let mut statement = self
            .connection
            .prepare(
                "SELECT operation_id FROM sftp_operation_journal ORDER BY created_at, operation_id",
            )
            .map_err(|_| journal_unavailable())?;
        let ids = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|_| journal_unavailable())?
            .map(|result| {
                result
                    .map_err(|_| journal_invalid())
                    .and_then(|value| Uuid::parse_str(&value).map_err(|_| journal_invalid()))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut records = Vec::with_capacity(ids.len());
        for operation_id in ids {
            let record = self.get(operation_id)?.ok_or_else(journal_invalid)?;
            if !record.state.is_terminal() {
                records.push(record);
            }
        }
        Ok(records)
    }

    pub fn delete(&self, operation_id: Uuid) -> Result<bool, JournalError> {
        self.connection
            .execute(
                "DELETE FROM sftp_operation_journal WHERE operation_id = ?1",
                [operation_id.to_string()],
            )
            .map(|count| count == 1)
            .map_err(|_| journal_unavailable())
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn insert_ciphertext_for_test(
        &self,
        operation_id: Uuid,
        ciphertext: &[u8],
    ) -> Result<(), JournalError> {
        self.connection
            .execute(
                "INSERT INTO sftp_operation_journal(operation_id, protected_record) VALUES (?1, ?2)",
                params![operation_id.to_string(), ciphertext],
            )
            .map_err(|_| journal_unavailable())?;
        Ok(())
    }
}

fn initialize(connection: &Connection) -> Result<(), JournalError> {
    let transaction = connection
        .unchecked_transaction()
        .map_err(|_| journal_unavailable())?;
    transaction
        .execute_batch(
            "CREATE TABLE sftp_operation_journal (
                operation_id TEXT PRIMARY KEY,
                protected_record BLOB NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                updated_at INTEGER NOT NULL DEFAULT (unixepoch())
            ) STRICT;",
        )
        .map_err(|_| journal_unavailable())?;
    transaction
        .pragma_update(None, "application_id", APPLICATION_ID)
        .map_err(|_| journal_unavailable())?;
    transaction
        .pragma_update(None, "user_version", SCHEMA_VERSION)
        .map_err(|_| journal_unavailable())?;
    transaction.commit().map_err(|_| journal_unavailable())
}

fn validate_schema(connection: &Connection) -> Result<(), JournalError> {
    let table_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
            [],
            |row| row.get(0),
        )
        .map_err(|_| journal_invalid())?;
    if table_count != 1 {
        return Err(journal_invalid());
    }
    let sql: Option<String> = connection
        .query_row(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'sftp_operation_journal'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| journal_invalid())?;
    let normalized = sql
        .as_deref()
        .map(|sql| {
            sql.split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .to_ascii_uppercase()
        })
        .ok_or_else(journal_invalid)?;
    for required in [
        "OPERATION_ID TEXT PRIMARY KEY",
        "PROTECTED_RECORD BLOB NOT NULL",
        "CREATED_AT INTEGER NOT NULL",
        "UPDATED_AT INTEGER NOT NULL",
        ") STRICT",
    ] {
        if !normalized.contains(required) {
            return Err(journal_invalid());
        }
    }
    Ok(())
}

fn journal_unavailable() -> JournalError {
    JournalError::new(
        "SFTP_JOURNAL_UNAVAILABLE",
        "The local SFTP journal is unavailable.",
    )
}

fn journal_invalid() -> JournalError {
    JournalError::new(
        "SFTP_JOURNAL_INVALID",
        "The local SFTP journal schema is invalid.",
    )
}

fn journal_encrypt_failed() -> JournalError {
    JournalError::new(
        "SFTP_JOURNAL_ENCRYPT_FAILED",
        "The local SFTP record could not be protected.",
    )
}

fn journal_decrypt_failed() -> JournalError {
    JournalError::new(
        "SFTP_JOURNAL_DECRYPT_FAILED",
        "The local SFTP record could not be authenticated.",
    )
}
