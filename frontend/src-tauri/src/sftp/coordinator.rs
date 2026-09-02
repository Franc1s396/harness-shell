use std::{
    collections::HashMap,
    future::Future,
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};

#[cfg(debug_assertions)]
use std::sync::Condvar;
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
#[cfg(debug_assertions)]
use tokio::sync::Semaphore;
use tokio::{
    sync::{mpsc, oneshot, Notify},
    time::Instant,
};
use uuid::Uuid;

#[cfg(debug_assertions)]
use super::journal::JournalFaultTestGate;
use super::{
    journal::{
        LocalSftpJournalActor, LocalSftpOperationJournal, LocalSftpOperationRecord, OperationKind,
        OperationState,
    },
    local_files::{
        freeze_upload_source, inspect_download_part, open_download_part_folder,
        prepare_download_target, FrozenUploadSource, LocalDownloadPartInspection, LocalPartFile,
        LocalTargetSnapshot, PreparedDownloadTarget,
    },
    models::{
        DeletePlanSummary, DownloadChunk, DownloadReady, EntryType, ManualSftpError,
        OperationPhase, OperationTerminalProjection, OperationTerminalState, RecoveryAction,
        RecoveryKind, RecoveryState, RecoverySummary, RemoteEntry, RemoteFileHash,
        TransferDirection, TransferProgressProjection, TransferSnapshot, UploadChunkAck,
        UploadReady, SFTP_CHUNK_BYTES,
    },
    protocol::{ManualSftpRuntimeClient, RecoveryResponse},
};

const PREPARATION_TTL: Duration = Duration::from_secs(5 * 60);
const MUTATING_REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
const RECURSIVE_REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
const SHUTDOWN_DRAIN_TIMEOUT: Duration = Duration::from_secs(3);

/// Typed boundary used by the coordinator to publish safe transfer progress.
///
/// Implementations receive only the path-free WebView projection. A sink failure is diagnostic:
/// it must never change or replay an already-running remote transfer.
pub trait TransferProgressSink: Send + Sync {
    fn emit(&self, projection: TransferProgressProjection)
        -> Result<(), TransferProgressSinkError>;
}

/// Stable, payload-free error returned by a transfer progress sink.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TransferProgressSinkError;

impl TransferProgressSinkError {
    pub const fn event_emit_failed() -> Self {
        Self
    }

    pub const fn code(self) -> &'static str {
        "SFTP_PROGRESS_EVENT_EMIT_FAILED"
    }
}

#[cfg(debug_assertions)]
struct DiscardingTransferProgressSink;

#[cfg(debug_assertions)]
impl TransferProgressSink for DiscardingTransferProgressSink {
    fn emit(
        &self,
        _projection: TransferProgressProjection,
    ) -> Result<(), TransferProgressSinkError> {
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CoordinatorLifecycleState {
    Open,
    Closing,
    Closed,
}

struct CoordinatorLifecycle {
    state: CoordinatorLifecycleState,
    active_workflows: usize,
}

/// Tracks cleanup work that outlives a cancelled command future. The count is incremented before
/// the detached abort is enqueued and is released only after the mutation owner has settled remote,
/// local-file, and journal state, so coordinator shutdown cannot overtake orphan prevention.
struct DetachedCleanupTracker {
    active: AtomicUsize,
    drain_notify: Arc<Notify>,
}

impl DetachedCleanupTracker {
    fn new(drain_notify: Arc<Notify>) -> Self {
        Self {
            active: AtomicUsize::new(0),
            drain_notify,
        }
    }

    fn begin(self: &Arc<Self>) -> DetachedCleanupGuard {
        self.active.fetch_add(1, Ordering::AcqRel);
        DetachedCleanupGuard {
            tracker: Arc::clone(self),
        }
    }

    fn active(&self) -> usize {
        self.active.load(Ordering::Acquire)
    }
}

struct DetachedCleanupGuard {
    tracker: Arc<DetachedCleanupTracker>,
}

impl Drop for DetachedCleanupGuard {
    fn drop(&mut self) {
        let previous = self.tracker.active.fetch_sub(1, Ordering::AcqRel);
        assert!(previous > 0, "manual SFTP detached-cleanup count underflow");
        if previous == 1 {
            self.tracker.drain_notify.notify_waiters();
        }
    }
}

/// Result of the bounded coordinator drain performed before Sidecar shutdown.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CoordinatorShutdownOutcome {
    drained: bool,
}

/// Deterministic contract-test checkpoint immediately before a mutating dispatch.
///
/// Production construction leaves this absent. The integration contract uses it to prove that a
/// workflow admitted before Closing cannot cross its first remote mutation boundary afterward.
#[doc(hidden)]
#[derive(Clone)]
#[cfg(debug_assertions)]
pub struct MutatingDispatchTestGate {
    reached: Arc<Semaphore>,
    closing_attempted: Arc<Semaphore>,
    closing_linearized: Arc<Semaphore>,
    release: Arc<(Mutex<bool>, Condvar)>,
    armed: Arc<AtomicBool>,
}

#[cfg(debug_assertions)]
impl MutatingDispatchTestGate {
    #[doc(hidden)]
    pub fn new() -> Self {
        Self {
            reached: Arc::new(Semaphore::new(0)),
            closing_attempted: Arc::new(Semaphore::new(0)),
            closing_linearized: Arc::new(Semaphore::new(0)),
            release: Arc::new((Mutex::new(false), Condvar::new())),
            armed: Arc::new(AtomicBool::new(true)),
        }
    }

    #[doc(hidden)]
    pub async fn wait_until_blocked(&self) {
        tokio::time::timeout(Duration::from_secs(5), self.reached.acquire())
            .await
            .expect("timed out waiting for mutating dispatch test gate")
            .expect("mutating dispatch test gate closed")
            .forget();
    }

    #[doc(hidden)]
    pub fn release(&self) {
        let (released, wake) = &*self.release;
        *released
            .lock()
            .expect("mutating dispatch test gate mutex poisoned") = true;
        wake.notify_all();
    }

    #[doc(hidden)]
    pub async fn wait_until_closing_attempted(&self) {
        tokio::time::timeout(Duration::from_secs(5), self.closing_attempted.acquire())
            .await
            .expect("timed out waiting for mutation closing-attempt gate")
            .expect("mutating dispatch closing-attempt gate closed")
            .forget();
    }

    #[doc(hidden)]
    pub fn closing_has_linearized(&self) -> bool {
        self.closing_linearized.available_permits() > 0
    }

    #[doc(hidden)]
    pub async fn wait_until_closing_linearized(&self) {
        tokio::time::timeout(Duration::from_secs(5), self.closing_linearized.acquire())
            .await
            .expect("timed out waiting for mutation closing-linearization gate")
            .expect("mutating dispatch closing-linearization gate closed")
            .forget();
    }

    fn mark_closing_attempted(&self) {
        self.closing_attempted.add_permits(1);
    }

    fn mark_closing_linearized(&self) {
        self.closing_linearized.add_permits(1);
    }

    fn block_after_check(&self) {
        if !self.armed.swap(false, Ordering::AcqRel) {
            return;
        }
        self.reached.add_permits(1);
        let (released, wake) = &*self.release;
        let mut released = released
            .lock()
            .expect("mutating dispatch test gate mutex poisoned");
        while !*released {
            let (next, wait) = wake
                .wait_timeout(released, Duration::from_secs(5))
                .expect("mutating dispatch test gate mutex poisoned");
            assert!(
                !wait.timed_out(),
                "timed out releasing mutating dispatch test gate"
            );
            released = next;
        }
    }
}

#[cfg(debug_assertions)]
impl Default for MutatingDispatchTestGate {
    fn default() -> Self {
        Self::new()
    }
}

/// Deterministic contract-test checkpoint after a local-file owner inserts a newly opened handle
/// and immediately before it sends the reply back to the async caller.
#[doc(hidden)]
#[derive(Clone)]
#[cfg(debug_assertions)]
pub struct LocalFileReplyTestGate {
    reached: Arc<Semaphore>,
    release: Arc<(Mutex<bool>, Condvar)>,
    armed: Arc<AtomicBool>,
}

#[cfg(debug_assertions)]
impl LocalFileReplyTestGate {
    #[doc(hidden)]
    pub fn new() -> Self {
        Self {
            reached: Arc::new(Semaphore::new(0)),
            release: Arc::new((Mutex::new(false), Condvar::new())),
            armed: Arc::new(AtomicBool::new(true)),
        }
    }

    #[doc(hidden)]
    pub async fn wait_until_blocked(&self) {
        tokio::time::timeout(Duration::from_secs(5), self.reached.acquire())
            .await
            .expect("timed out waiting for local-file reply test gate")
            .expect("local-file reply test gate closed")
            .forget();
    }

    #[doc(hidden)]
    pub fn release(&self) {
        let (released, wake) = &*self.release;
        *released
            .lock()
            .expect("local-file reply test gate mutex poisoned") = true;
        wake.notify_all();
    }

    fn block_after_insert(&self) {
        if !self.armed.swap(false, Ordering::AcqRel) {
            return;
        }
        self.reached.add_permits(1);
        let (released, wake) = &*self.release;
        let mut released = released
            .lock()
            .expect("local-file reply test gate mutex poisoned");
        while !*released {
            let (next, wait) = wake
                .wait_timeout(released, Duration::from_secs(5))
                .expect("local-file reply test gate mutex poisoned");
            assert!(
                !wait.timed_out(),
                "timed out releasing local-file reply test gate"
            );
            released = next;
        }
    }
}

#[cfg(debug_assertions)]
impl Default for LocalFileReplyTestGate {
    fn default() -> Self {
        Self::new()
    }
}

/// Deterministic test-only fault injected at the blocking local-file owner's finish boundary.
#[doc(hidden)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg(debug_assertions)]
pub enum LocalFileFinishFault {
    UploadRead,
    PartCreate,
    PartWrite,
    PartAbort,
    Sync,
    AtomicCommit,
}

/// One-shot fault seam used to verify coordinator durability after local commit failures.
#[doc(hidden)]
#[derive(Clone)]
#[cfg(debug_assertions)]
pub struct LocalFileFinishTestGate {
    next: Arc<Mutex<Vec<LocalFileFinishFault>>>,
    block_next: Arc<AtomicBool>,
    reached: Arc<Semaphore>,
    release: Arc<(Mutex<bool>, Condvar)>,
}

#[cfg(debug_assertions)]
impl LocalFileFinishTestGate {
    #[doc(hidden)]
    pub fn fail_next(&self, fault: LocalFileFinishFault) {
        let mut next = self
            .next
            .lock()
            .expect("local-file finish test gate mutex poisoned");
        next.clear();
        next.push(fault);
    }

    #[doc(hidden)]
    pub fn fail_sequence(&self, faults: &[LocalFileFinishFault]) {
        *self
            .next
            .lock()
            .expect("local-file finish test gate mutex poisoned") = faults.to_vec();
    }

    fn take(&self) -> Option<LocalFileFinishFault> {
        let mut next = self
            .next
            .lock()
            .expect("local-file finish test gate mutex poisoned");
        if next.is_empty() {
            None
        } else {
            Some(next.remove(0))
        }
    }

    fn take_if(&self, expected: LocalFileFinishFault) -> bool {
        let mut next = self
            .next
            .lock()
            .expect("local-file finish test gate mutex poisoned");
        if next.first() == Some(&expected) {
            next.remove(0);
            true
        } else {
            false
        }
    }

    #[doc(hidden)]
    pub fn block_next_finish(&self) {
        self.block_next.store(true, Ordering::Release);
        *self
            .release
            .0
            .lock()
            .expect("local-file finish test gate mutex poisoned") = false;
    }

    #[doc(hidden)]
    pub async fn wait_until_blocked(&self) {
        tokio::time::timeout(Duration::from_secs(5), self.reached.acquire())
            .await
            .expect("timed out waiting for local-file finish test gate")
            .expect("local-file finish test gate closed")
            .forget();
    }

    #[doc(hidden)]
    pub fn release(&self) {
        *self
            .release
            .0
            .lock()
            .expect("local-file finish test gate mutex poisoned") = true;
        self.release.1.notify_all();
    }

    fn block_if_armed(&self) {
        if !self.block_next.swap(false, Ordering::AcqRel) {
            return;
        }
        self.reached.add_permits(1);
        let mut released = self
            .release
            .0
            .lock()
            .expect("local-file finish test gate mutex poisoned");
        while !*released {
            let (next, wait) = self
                .release
                .1
                .wait_timeout(released, Duration::from_secs(5))
                .expect("local-file finish test gate mutex poisoned");
            assert!(
                !wait.timed_out(),
                "timed out releasing local-file finish test gate"
            );
            released = next;
        }
    }
}

#[cfg(debug_assertions)]
impl Default for LocalFileFinishTestGate {
    fn default() -> Self {
        Self {
            next: Arc::new(Mutex::new(Vec::new())),
            block_next: Arc::new(AtomicBool::new(false)),
            reached: Arc::new(Semaphore::new(0)),
            release: Arc::new((Mutex::new(false), Condvar::new())),
        }
    }
}

/// Opaque reference to an upload source owned exclusively by the local-file worker.
struct FrozenUploadHandle {
    handle_id: Uuid,
    path: PathBuf,
    display_name: String,
    byte_count: u64,
    sha256: String,
}

/// Dedicated owner for every open `FrozenUploadSource` and `LocalPartFile` handle.
#[derive(Clone)]
struct LocalFileActor {
    commands: mpsc::UnboundedSender<LocalFileCommand>,
}

enum LocalFileCommand {
    FreezeUpload {
        handle_id: Uuid,
        path: PathBuf,
        reply: oneshot::Sender<Result<FrozenUploadHandle, ManualSftpError>>,
    },
    ReadUpload {
        handle_id: Uuid,
        maximum: usize,
        reply: oneshot::Sender<Result<Vec<u8>, ManualSftpError>>,
    },
    CloseUpload {
        handle_id: Uuid,
    },
    PrepareDownloadTarget {
        path: PathBuf,
        reply: oneshot::Sender<Result<PreparedDownloadTarget, ManualSftpError>>,
    },
    CreatePart {
        handle_id: Uuid,
        target: PreparedDownloadTarget,
        operation_id: Uuid,
        reply: oneshot::Sender<Result<(), ManualSftpError>>,
    },
    WritePart {
        handle_id: Uuid,
        bytes: Vec<u8>,
        reply: oneshot::Sender<Result<(), ManualSftpError>>,
    },
    AbortPart {
        handle_id: Uuid,
        reply: oneshot::Sender<Result<(), ManualSftpError>>,
    },
    FinishPart {
        handle_id: Uuid,
        expected_sha256: String,
        reply: oneshot::Sender<Result<(), ManualSftpError>>,
    },
    ClosePart {
        handle_id: Uuid,
    },
    InspectDownloadPart {
        target_path: PathBuf,
        operation_id: Uuid,
        expected_sha256: String,
        reply: oneshot::Sender<Result<LocalDownloadPartInspection, ManualSftpError>>,
    },
    OpenDownloadPartFolder {
        target_path: PathBuf,
        operation_id: Uuid,
        expected_sha256: String,
        reply: oneshot::Sender<Result<LocalDownloadPartInspection, ManualSftpError>>,
    },
    #[cfg(debug_assertions)]
    OwnerCount {
        reply: oneshot::Sender<usize>,
    },
}

impl LocalFileActor {
    fn spawn(
        #[cfg(debug_assertions)] reply_test_gate: Option<LocalFileReplyTestGate>,
        #[cfg(debug_assertions)] finish_test_gate: Option<LocalFileFinishTestGate>,
    ) -> Self {
        let (commands, mut receiver) = mpsc::unbounded_channel();
        thread::Builder::new()
            .name("manual-sftp-local-files".to_owned())
            .spawn(move || {
                let mut uploads = HashMap::<Uuid, FrozenUploadSource>::new();
                let mut parts = HashMap::<Uuid, LocalPartFile>::new();
                while let Some(command) = receiver.blocking_recv() {
                    match command {
                        LocalFileCommand::FreezeUpload {
                            handle_id,
                            path,
                            reply,
                        } => match freeze_upload_source(&path).map_err(local_error) {
                            Ok(source) => {
                                let handle = FrozenUploadHandle {
                                    handle_id,
                                    path: source.path().to_path_buf(),
                                    display_name: source.display_name().to_owned(),
                                    byte_count: source.byte_count(),
                                    sha256: source.sha256().to_owned(),
                                };
                                uploads.insert(handle_id, source);
                                #[cfg(debug_assertions)]
                                if let Some(gate) = &reply_test_gate {
                                    gate.block_after_insert();
                                }
                                if reply.send(Ok(handle)).is_err() {
                                    drop(uploads.remove(&handle_id));
                                }
                            }
                            Err(error) => {
                                let _ = reply.send(Err(error));
                            }
                        },
                        LocalFileCommand::ReadUpload {
                            handle_id,
                            maximum,
                            reply,
                        } => {
                            #[cfg(debug_assertions)]
                            let result = match finish_test_gate.as_ref().and_then(|gate| gate.take())
                            {
                                Some(LocalFileFinishFault::UploadRead) => Err(ManualSftpError::new(
                                    "SFTP_LOCAL_READ_FAILED",
                                    "The frozen upload source could not be read.",
                                )),
                                Some(fault) => {
                                    if let Some(gate) = &finish_test_gate {
                                        gate.fail_next(fault);
                                    }
                                    uploads
                                        .get_mut(&handle_id)
                                        .ok_or_else(local_handle_invalid)
                                        .and_then(|source| {
                                            source.read_chunk(maximum).map_err(local_error)
                                        })
                                }
                                None => uploads
                                    .get_mut(&handle_id)
                                    .ok_or_else(local_handle_invalid)
                                    .and_then(|source| {
                                        source.read_chunk(maximum).map_err(local_error)
                                    }),
                            };
                            #[cfg(not(debug_assertions))]
                            let result = uploads
                                .get_mut(&handle_id)
                                .ok_or_else(local_handle_invalid)
                                .and_then(|source| source.read_chunk(maximum).map_err(local_error));
                            let _ = reply.send(result);
                        }
                        LocalFileCommand::CloseUpload { handle_id } => {
                            drop(uploads.remove(&handle_id));
                        }
                        LocalFileCommand::PrepareDownloadTarget { path, reply } => {
                            let _ = reply.send(prepare_download_target(&path).map_err(local_error));
                        }
                        LocalFileCommand::CreatePart {
                            handle_id,
                            target,
                            operation_id,
                            reply,
                        } => {
                            #[cfg(debug_assertions)]
                            let injected_failure = finish_test_gate
                                .as_ref()
                                .is_some_and(|gate| gate.take_if(LocalFileFinishFault::PartCreate));
                            #[cfg(not(debug_assertions))]
                            let injected_failure = false;
                            let result = if injected_failure {
                                Err(ManualSftpError::new(
                                    "SFTP_LOCAL_PART_CREATE_FAILED",
                                    "The local download part could not be created.",
                                ))
                            } else if parts.contains_key(&handle_id) {
                                Err(local_handle_invalid())
                            } else {
                                LocalPartFile::create(target, operation_id)
                                    .map_err(local_error)
                                    .map(|part| {
                                        parts.insert(handle_id, part);
                                    })
                            };
                            if result.is_ok() {
                                #[cfg(debug_assertions)]
                                if let Some(gate) = &reply_test_gate {
                                    gate.block_after_insert();
                                }
                                if reply.send(result).is_err() {
                                    drop(parts.remove(&handle_id));
                                }
                            } else {
                                let _ = reply.send(result);
                            }
                        }
                        LocalFileCommand::WritePart {
                            handle_id,
                            bytes,
                            reply,
                        } => {
                            #[cfg(debug_assertions)]
                            let injected_failure = finish_test_gate
                                .as_ref()
                                .is_some_and(|gate| gate.take_if(LocalFileFinishFault::PartWrite));
                            #[cfg(not(debug_assertions))]
                            let injected_failure = false;
                            let result = if injected_failure {
                                Err(ManualSftpError::new(
                                    "SFTP_LOCAL_WRITE_FAILED",
                                    "The local download part could not be written.",
                                ))
                            } else {
                                parts
                                    .get_mut(&handle_id)
                                    .ok_or_else(local_handle_invalid)
                                    .and_then(|part| {
                                        part.write_all(&bytes).map_err(local_error_from_io)
                                    })
                            };
                            let _ = reply.send(result);
                        }
                        LocalFileCommand::AbortPart { handle_id, reply } => {
                            #[cfg(debug_assertions)]
                            let injected_failure = finish_test_gate
                                .as_ref()
                                .is_some_and(|gate| gate.take_if(LocalFileFinishFault::PartAbort));
                            #[cfg(not(debug_assertions))]
                            let injected_failure = false;
                            let result = if injected_failure {
                                Err(ManualSftpError::new(
                                    "SFTP_LOCAL_CLEANUP_FAILED",
                                    "The local download part could not be removed.",
                                ))
                            } else {
                                parts
                                    .remove(&handle_id)
                                    .ok_or_else(local_handle_invalid)
                                    .and_then(|part| part.abort().map_err(local_error))
                            };
                            let _ = reply.send(result);
                        }
                        LocalFileCommand::FinishPart {
                            handle_id,
                            expected_sha256,
                            reply,
                        } => {
                            #[cfg(debug_assertions)]
                            if let Some(gate) = &finish_test_gate {
                                gate.block_if_armed();
                            }
                            let part = parts.remove(&handle_id).ok_or_else(local_handle_invalid);
                            #[cfg(debug_assertions)]
                            let result = match finish_test_gate.as_ref().and_then(|gate| gate.take())
                            {
                                Some(
                                    LocalFileFinishFault::UploadRead
                                    | LocalFileFinishFault::PartCreate
                                    | LocalFileFinishFault::PartWrite
                                    | LocalFileFinishFault::PartAbort,
                                ) => part.and_then(|part| {
                                    part.finish(&expected_sha256).map_err(local_error)
                                }),
                                Some(LocalFileFinishFault::Sync) => part.and_then(|_part| {
                                    Err(ManualSftpError::new(
                                        "SFTP_LOCAL_SYNC_FAILED",
                                        "The local download part could not be synchronized.",
                                    ))
                                }),
                                Some(LocalFileFinishFault::AtomicCommit) => {
                                    part.and_then(|_part| {
                                        Err(ManualSftpError::new(
                                            "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
                                            "The local file system could not perform the required atomic replace.",
                                        ))
                                    })
                                }
                                None => part.and_then(|part| {
                                    part.finish(&expected_sha256).map_err(local_error)
                                }),
                            };
                            #[cfg(not(debug_assertions))]
                            let result = part.and_then(|part| {
                                part.finish(&expected_sha256).map_err(local_error)
                            });
                            let _ = reply.send(result);
                        }
                        LocalFileCommand::ClosePart { handle_id } => {
                            drop(parts.remove(&handle_id));
                        }
                        LocalFileCommand::InspectDownloadPart {
                            target_path,
                            operation_id,
                            expected_sha256,
                            reply,
                        } => {
                            let result = inspect_download_part(
                                &target_path,
                                operation_id,
                                &expected_sha256,
                            )
                            .map_err(local_error);
                            let _ = reply.send(result);
                        }
                        LocalFileCommand::OpenDownloadPartFolder {
                            target_path,
                            operation_id,
                            expected_sha256,
                            reply,
                        } => {
                            let result = open_download_part_folder(
                                &target_path,
                                operation_id,
                                &expected_sha256,
                            )
                            .map_err(local_error);
                            let _ = reply.send(result);
                        }
                        #[cfg(debug_assertions)]
                        LocalFileCommand::OwnerCount { reply } => {
                            let _ = reply.send(uploads.len() + parts.len());
                        }
                    }
                }
            })
            .expect("failed to start the manual SFTP local-file worker");
        Self { commands }
    }

    async fn freeze_upload(
        &self,
        handle_id: Uuid,
        path: PathBuf,
    ) -> Result<FrozenUploadHandle, ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::FreezeUpload {
                handle_id,
                path,
                reply,
            })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    async fn read_upload(
        &self,
        handle_id: Uuid,
        maximum: usize,
    ) -> Result<Vec<u8>, ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::ReadUpload {
                handle_id,
                maximum,
                reply,
            })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    fn close_upload(&self, handle_id: Uuid) {
        let _ = self
            .commands
            .send(LocalFileCommand::CloseUpload { handle_id });
    }

    async fn prepare_download_target(
        &self,
        path: PathBuf,
    ) -> Result<PreparedDownloadTarget, ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::PrepareDownloadTarget { path, reply })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    async fn create_part(
        &self,
        handle_id: Uuid,
        target: PreparedDownloadTarget,
        operation_id: Uuid,
    ) -> Result<(), ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::CreatePart {
                handle_id,
                target,
                operation_id,
                reply,
            })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    async fn write_part(&self, handle_id: Uuid, bytes: Vec<u8>) -> Result<(), ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::WritePart {
                handle_id,
                bytes,
                reply,
            })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    async fn abort_part(&self, handle_id: Uuid) -> Result<(), ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::AbortPart { handle_id, reply })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    async fn finish_part(
        &self,
        handle_id: Uuid,
        expected_sha256: String,
    ) -> Result<(), ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::FinishPart {
                handle_id,
                expected_sha256,
                reply,
            })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    fn close_part(&self, handle_id: Uuid) {
        let _ = self
            .commands
            .send(LocalFileCommand::ClosePart { handle_id });
    }

    async fn inspect_download_part(
        &self,
        target_path: PathBuf,
        operation_id: Uuid,
        expected_sha256: String,
    ) -> Result<LocalDownloadPartInspection, ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::InspectDownloadPart {
                target_path,
                operation_id,
                expected_sha256,
                reply,
            })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    async fn open_download_part_folder(
        &self,
        target_path: PathBuf,
        operation_id: Uuid,
        expected_sha256: String,
    ) -> Result<LocalDownloadPartInspection, ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::OpenDownloadPartFolder {
                target_path,
                operation_id,
                expected_sha256,
                reply,
            })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())?
    }

    #[cfg(debug_assertions)]
    async fn owner_count(&self) -> Result<usize, ManualSftpError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(LocalFileCommand::OwnerCount { reply })
            .map_err(|_| local_worker_failed())?;
        response.await.map_err(|_| local_worker_failed())
    }
}

/// Single async owner for forward and cleanup mutation dispatches. The coordinator performs the
/// lifecycle check and the non-waiting actor enqueue in one synchronous critical section; this
/// actor is then the only component allowed to call mutating runtime methods.
struct MutationDispatchActor {
    commands: mpsc::UnboundedSender<MutationDispatchCommand>,
    owner: Mutex<Option<MutationDispatchOwner>>,
}

struct MutationDispatchOwner {
    runtime: ManualSftpRuntimeClient,
    journal: LocalSftpJournalActor,
    local_files: LocalFileActor,
    #[cfg(debug_assertions)]
    mutation_diagnostics: Arc<Mutex<Vec<MutationDiagnostic>>>,
    receiver: mpsc::UnboundedReceiver<MutationDispatchCommand>,
}

enum MutationDispatchCommand {
    Request(MutationRequestCommand),
    DetachedTransferAbort {
        operation_id: Uuid,
        direction: TransferDirection,
        part_handle_id: Option<Uuid>,
        cleanup: DetachedCleanupGuard,
    },
}

struct MutationRequestCommand {
    request: MutationDispatchRequest,
    uncertain_operation_id: Option<Uuid>,
    retain_on_pre_dispatch_cancel: bool,
    started: oneshot::Sender<Result<(), ManualSftpError>>,
    reply: oneshot::Sender<Result<MutationDispatchResponse, ManualSftpError>>,
}

struct MutationDispatchHandle {
    started: oneshot::Receiver<Result<(), ManualSftpError>>,
    response: oneshot::Receiver<Result<MutationDispatchResponse, ManualSftpError>>,
}

/// Redacted internal evidence for a post-dispatch uncertainty transition.
#[doc(hidden)]
#[derive(Clone, Debug, Eq, PartialEq)]
#[cfg(debug_assertions)]
pub struct MutationDiagnostic {
    pub operation_id: Uuid,
    pub cause_code: String,
    pub journal_error_code: Option<String>,
}

enum MutationDispatchRequest {
    Mkdir {
        operation_id: Uuid,
        ssh_session_id: Uuid,
        parent_path: String,
        name: String,
    },
    Rename {
        operation_id: Uuid,
        ssh_session_id: Uuid,
        source_path: String,
        target_path: String,
        overwrite: bool,
        source_snapshot: Option<TransferSnapshot>,
        target_snapshot: Option<TransferSnapshot>,
    },
    Remove {
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: String,
        expected_snapshot: TransferSnapshot,
    },
    DeletePreflight {
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: String,
    },
    DeleteExecute {
        delete_plan_id: Uuid,
    },
    RecoveryExecute {
        recovery_id: Uuid,
        action: RecoveryAction,
        operation_id: Uuid,
    },
    UploadBegin {
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: String,
        source_sha256: String,
        source_byte_count: u64,
        target_snapshot: TransferSnapshot,
    },
    UploadChunk {
        operation_id: Uuid,
        sequence: u32,
        offset: u64,
        chunk: Vec<u8>,
    },
    UploadFinish {
        operation_id: Uuid,
    },
    UploadAbort {
        operation_id: Uuid,
    },
    DownloadBegin {
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: String,
    },
    DownloadChunk {
        operation_id: Uuid,
        sequence: u32,
        offset: u64,
    },
    DownloadFinish {
        operation_id: Uuid,
    },
    DownloadAbort {
        operation_id: Uuid,
    },
}

enum MutationDispatchResponse {
    Terminal(OperationTerminalProjection),
    Recovery(RecoveryResponse),
    UploadReady(UploadReady),
    UploadChunk(UploadChunkAck),
    DownloadReady(DownloadReady),
    DownloadChunk(DownloadChunk),
    DeletePlan(DeletePlanSummary),
}

#[derive(Clone, Copy)]
enum DroppedReplyPolicy {
    Terminal,
    NonTerminal,
    Recovery,
}

impl MutationDispatchRequest {
    fn dropped_reply_policy(&self) -> DroppedReplyPolicy {
        match self {
            Self::Mkdir { .. }
            | Self::Rename { .. }
            | Self::Remove { .. }
            | Self::DeletePreflight { .. }
            | Self::DeleteExecute { .. }
            | Self::UploadFinish { .. }
            | Self::UploadAbort { .. }
            | Self::DownloadFinish { .. }
            | Self::DownloadAbort { .. } => DroppedReplyPolicy::Terminal,
            Self::RecoveryExecute { .. } => DroppedReplyPolicy::Recovery,
            Self::UploadBegin { .. }
            | Self::UploadChunk { .. }
            | Self::DownloadBegin { .. }
            | Self::DownloadChunk { .. } => DroppedReplyPolicy::NonTerminal,
        }
    }
}

impl MutationDispatchActor {
    fn new(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpJournalActor,
        local_files: LocalFileActor,
        #[cfg(debug_assertions)] mutation_diagnostics: Arc<Mutex<Vec<MutationDiagnostic>>>,
    ) -> Self {
        let (commands, receiver) = mpsc::unbounded_channel::<MutationDispatchCommand>();
        Self {
            commands,
            owner: Mutex::new(Some(MutationDispatchOwner {
                runtime,
                journal,
                local_files,
                #[cfg(debug_assertions)]
                mutation_diagnostics,
                receiver,
            })),
        }
    }

    fn ensure_started(&self) {
        let owner = self
            .owner
            .lock()
            .expect("manual SFTP mutation-dispatch owner mutex poisoned")
            .take();
        if let Some(owner) = owner {
            // The first dispatch always occurs while an async Tauri command is being polled, so
            // the owner is attached to that command runtime rather than a separate global runtime.
            tokio::spawn(run_mutation_dispatch_owner(owner));
        }
    }

    fn enqueue(
        &self,
        request: MutationDispatchRequest,
        uncertain_operation_id: Option<Uuid>,
    ) -> Result<MutationDispatchHandle, ManualSftpError> {
        self.ensure_started();
        let retain_on_pre_dispatch_cancel =
            matches!(&request, MutationDispatchRequest::DeleteExecute { .. });
        let (started, start_status) = oneshot::channel();
        let (reply, response) = oneshot::channel();
        self.commands
            .send(MutationDispatchCommand::Request(MutationRequestCommand {
                request,
                uncertain_operation_id,
                retain_on_pre_dispatch_cancel,
                started,
                reply,
            }))
            .map_err(|_| mutation_dispatch_worker_failed())?;
        Ok(MutationDispatchHandle {
            started: start_status,
            response,
        })
    }

    fn enqueue_detached_transfer_abort(
        &self,
        operation_id: Uuid,
        direction: TransferDirection,
        part_handle_id: Option<Uuid>,
        cleanup: DetachedCleanupGuard,
    ) -> Result<(), ManualSftpError> {
        self.ensure_started();
        self.commands
            .send(MutationDispatchCommand::DetachedTransferAbort {
                operation_id,
                direction,
                part_handle_id,
                cleanup,
            })
            .map_err(|_| mutation_dispatch_worker_failed())
    }
}

async fn run_mutation_dispatch_owner(mut owner: MutationDispatchOwner) {
    while let Some(command) = owner.receiver.recv().await {
        let command = match command {
            MutationDispatchCommand::Request(command) => command,
            MutationDispatchCommand::DetachedTransferAbort {
                operation_id,
                direction,
                part_handle_id,
                cleanup,
            } => {
                let _cleanup = cleanup;
                settle_detached_transfer_abort(&owner, operation_id, direction, part_handle_id)
                    .await;
                continue;
            }
        };
        let reply = command.reply;
        let uncertain_operation_id = command.uncertain_operation_id;
        if let Some(operation_id) = uncertain_operation_id {
            if let Err(journal_error_code) =
                persist_operation_unknown(&owner.journal, operation_id).await
            {
                record_mutation_diagnostic(
                    #[cfg(debug_assertions)]
                    &owner.mutation_diagnostics,
                    operation_id,
                    "JOURNAL_BEFORE_MUTATION_DISPATCH_FAILED",
                    Some(journal_error_code),
                );
                let _ = command.started.send(Err(ManualSftpError::new(
                    "SFTP_JOURNAL_UNAVAILABLE",
                    "The manual SFTP operation journal is unavailable.",
                )));
                continue;
            }
        }
        if command.started.send(Ok(())).is_err() {
            if let Some(operation_id) = uncertain_operation_id {
                if command.retain_on_pre_dispatch_cancel {
                    persist_pre_dispatch_preparing(&owner, operation_id).await;
                } else {
                    persist_pre_dispatch_cancelled(&owner, operation_id).await;
                }
            }
            continue;
        }
        let dropped_reply_policy = command.request.dropped_reply_policy();
        let request = async {
            match command.request {
                MutationDispatchRequest::Mkdir {
                    operation_id,
                    ssh_session_id,
                    parent_path,
                    name,
                } => owner
                    .runtime
                    .mkdir(operation_id, ssh_session_id, &parent_path, &name)
                    .await
                    .map(MutationDispatchResponse::Terminal),
                MutationDispatchRequest::Rename {
                    operation_id,
                    ssh_session_id,
                    source_path,
                    target_path,
                    overwrite,
                    source_snapshot,
                    target_snapshot,
                } => owner
                    .runtime
                    .rename(
                        operation_id,
                        ssh_session_id,
                        &source_path,
                        &target_path,
                        overwrite,
                        source_snapshot.as_ref(),
                        target_snapshot.as_ref(),
                    )
                    .await
                    .map(MutationDispatchResponse::Terminal),
                MutationDispatchRequest::Remove {
                    operation_id,
                    ssh_session_id,
                    path,
                    expected_snapshot,
                } => owner
                    .runtime
                    .remove(operation_id, ssh_session_id, &path, &expected_snapshot)
                    .await
                    .map(MutationDispatchResponse::Terminal),
                MutationDispatchRequest::DeletePreflight {
                    operation_id,
                    ssh_session_id,
                    path,
                } => owner
                    .runtime
                    .delete_preflight(operation_id, ssh_session_id, &path)
                    .await
                    .map(MutationDispatchResponse::DeletePlan),
                MutationDispatchRequest::DeleteExecute { delete_plan_id } => owner
                    .runtime
                    .delete_execute(delete_plan_id)
                    .await
                    .map(MutationDispatchResponse::Terminal),
                MutationDispatchRequest::RecoveryExecute {
                    recovery_id,
                    action,
                    operation_id,
                } => owner
                    .runtime
                    .recovery_execute(recovery_id, action, operation_id)
                    .await
                    .map(MutationDispatchResponse::Recovery),
                MutationDispatchRequest::UploadBegin {
                    operation_id,
                    ssh_session_id,
                    path,
                    source_sha256,
                    source_byte_count,
                    target_snapshot,
                } => owner
                    .runtime
                    .upload_begin(
                        operation_id,
                        ssh_session_id,
                        &path,
                        &source_sha256,
                        source_byte_count,
                        &target_snapshot,
                    )
                    .await
                    .map(MutationDispatchResponse::UploadReady),
                MutationDispatchRequest::UploadChunk {
                    operation_id,
                    sequence,
                    offset,
                    chunk,
                } => owner
                    .runtime
                    .upload_chunk(operation_id, sequence, offset, &chunk)
                    .await
                    .map(MutationDispatchResponse::UploadChunk),
                MutationDispatchRequest::UploadFinish { operation_id } => owner
                    .runtime
                    .upload_finish(operation_id)
                    .await
                    .map(MutationDispatchResponse::Terminal),
                MutationDispatchRequest::UploadAbort { operation_id } => owner
                    .runtime
                    .upload_abort(operation_id)
                    .await
                    .map(MutationDispatchResponse::Terminal),
                MutationDispatchRequest::DownloadBegin {
                    operation_id,
                    ssh_session_id,
                    path,
                } => owner
                    .runtime
                    .download_begin(operation_id, ssh_session_id, &path)
                    .await
                    .map(MutationDispatchResponse::DownloadReady),
                MutationDispatchRequest::DownloadChunk {
                    operation_id,
                    sequence,
                    offset,
                } => owner
                    .runtime
                    .download_chunk(operation_id, sequence, offset)
                    .await
                    .map(MutationDispatchResponse::DownloadChunk),
                MutationDispatchRequest::DownloadFinish { operation_id } => owner
                    .runtime
                    .download_finish(operation_id)
                    .await
                    .map(MutationDispatchResponse::Terminal),
                MutationDispatchRequest::DownloadAbort { operation_id } => owner
                    .runtime
                    .download_abort(operation_id)
                    .await
                    .map(MutationDispatchResponse::Terminal),
            }
        };
        let result = request.await;
        if let Err(dropped_result) = reply.send(result) {
            if let Some(operation_id) = uncertain_operation_id {
                reconcile_dropped_dispatch_result(
                    &owner,
                    operation_id,
                    dropped_reply_policy,
                    dropped_result,
                )
                .await;
            }
        }
    }
}

async fn reconcile_dropped_dispatch_result(
    owner: &MutationDispatchOwner,
    operation_id: Uuid,
    policy: DroppedReplyPolicy,
    result: Result<MutationDispatchResponse, ManualSftpError>,
) {
    let state = match (policy, result) {
        (_, Ok(MutationDispatchResponse::Terminal(terminal)))
            if terminal.operation_id == operation_id =>
        {
            state_from_terminal(terminal.state)
        }
        (
            DroppedReplyPolicy::Recovery,
            Ok(MutationDispatchResponse::Recovery(RecoveryResponse::Terminal(terminal))),
        ) if terminal.operation_id == operation_id => state_from_terminal(terminal.state),
        (DroppedReplyPolicy::Terminal, Err(error)) if error.is_trusted_remote() => error
            .retained_operation_state()
            .map(|state| match state {
                super::models::RetainedOperationState::CleanupRequired => {
                    OperationState::CleanupRequired
                }
                super::models::RetainedOperationState::OutcomeUnknown => {
                    OperationState::OutcomeUnknown
                }
            })
            .unwrap_or(OperationState::Failed),
        _ => OperationState::OutcomeUnknown,
    };
    let transition = persist_dropped_dispatch_state(&owner.journal, operation_id, state).await;
    record_mutation_diagnostic(
        #[cfg(debug_assertions)]
        &owner.mutation_diagnostics,
        operation_id,
        "CALLER_FUTURE_DROPPED_AFTER_DISPATCH",
        transition.err(),
    );
}

async fn persist_dropped_dispatch_state(
    journal: &LocalSftpJournalActor,
    operation_id: Uuid,
    state: OperationState,
) -> Result<(), String> {
    let Some(mut record) = journal
        .get(operation_id)
        .await
        .map_err(|error| error.code().to_owned())?
    else {
        return Ok(());
    };
    record.state = state;
    journal
        .put(record)
        .await
        .map_err(|error| error.code().to_owned())?;
    if matches!(
        state,
        OperationState::Succeeded | OperationState::Failed | OperationState::Cancelled
    ) {
        let deleted = journal
            .delete(operation_id)
            .await
            .map_err(|error| error.code().to_owned())?;
        if !deleted {
            return Err("SFTP_JOURNAL_INVARIANT".to_owned());
        }
    }
    Ok(())
}

async fn settle_detached_transfer_abort(
    owner: &MutationDispatchOwner,
    operation_id: Uuid,
    direction: TransferDirection,
    part_handle_id: Option<Uuid>,
) {
    let journal_record = owner.journal.get(operation_id).await;
    let should_abort_remote = matches!(journal_record, Ok(Some(_)));
    let remote = if should_abort_remote {
        match direction {
            TransferDirection::Upload => owner.runtime.upload_abort(operation_id).await,
            TransferDirection::Download => owner.runtime.download_abort(operation_id).await,
        }
    } else {
        Err(ManualSftpError::new(
            "SFTP_OPERATION_ALREADY_FINALIZED",
            "The remote transfer operation is already finalized.",
        ))
    };
    let remote_confirmed = matches!(
        &remote,
        Ok(terminal)
            if terminal.operation_id == operation_id
                && terminal.state == OperationTerminalState::Cancelled
    );
    let local_cleanup = match part_handle_id {
        Some(handle_id) => owner.local_files.abort_part(handle_id).await,
        None => Ok(()),
    };
    let state = if local_cleanup.is_err() {
        OperationState::CleanupRequired
    } else if remote_confirmed {
        OperationState::Cancelled
    } else {
        OperationState::OutcomeUnknown
    };
    let transition = async {
        let Some(mut record) = owner
            .journal
            .get(operation_id)
            .await
            .map_err(|error| error.code().to_owned())?
        else {
            return Ok(());
        };
        record.state = state;
        owner
            .journal
            .put(record)
            .await
            .map_err(|error| error.code().to_owned())?;
        if state == OperationState::Cancelled {
            let deleted = owner
                .journal
                .delete(operation_id)
                .await
                .map_err(|error| error.code().to_owned())?;
            if !deleted {
                return Err("SFTP_JOURNAL_INVARIANT".to_owned());
            }
        }
        Ok(())
    }
    .await;
    let cause_code = if local_cleanup.is_err() {
        "DETACHED_TRANSFER_LOCAL_CLEANUP_FAILED"
    } else if remote_confirmed {
        "DETACHED_TRANSFER_ABORT_CONFIRMED"
    } else {
        "DETACHED_TRANSFER_ABORT_UNCONFIRMED"
    };
    record_mutation_diagnostic(
        #[cfg(debug_assertions)]
        &owner.mutation_diagnostics,
        operation_id,
        cause_code,
        transition.err(),
    );
}

async fn persist_pre_dispatch_cancelled(owner: &MutationDispatchOwner, operation_id: Uuid) {
    let transition = async {
        let mut record = owner
            .journal
            .get(operation_id)
            .await
            .map_err(|error| error.code().to_owned())?
            .ok_or_else(|| "SFTP_JOURNAL_RECORD_MISSING".to_owned())?;
        record.state = OperationState::Cancelled;
        owner
            .journal
            .put(record)
            .await
            .map_err(|error| error.code().to_owned())?;
        let deleted = owner
            .journal
            .delete(operation_id)
            .await
            .map_err(|error| error.code().to_owned())?;
        if !deleted {
            return Err("SFTP_JOURNAL_INVARIANT".to_owned());
        }
        Ok(())
    }
    .await;
    record_mutation_diagnostic(
        #[cfg(debug_assertions)]
        &owner.mutation_diagnostics,
        operation_id,
        "CALLER_DROPPED_BEFORE_MUTATION_DISPATCH",
        transition.err(),
    );
}

async fn persist_pre_dispatch_preparing(owner: &MutationDispatchOwner, operation_id: Uuid) {
    let transition = async {
        let mut record = owner
            .journal
            .get(operation_id)
            .await
            .map_err(|error| error.code().to_owned())?
            .ok_or_else(|| "SFTP_JOURNAL_RECORD_MISSING".to_owned())?;
        record.state = OperationState::Preparing;
        owner
            .journal
            .put(record)
            .await
            .map_err(|error| error.code().to_owned())
    }
    .await;
    record_mutation_diagnostic(
        #[cfg(debug_assertions)]
        &owner.mutation_diagnostics,
        operation_id,
        "CALLER_DROPPED_BEFORE_DELETE_PLAN_CONSUMPTION",
        transition.err(),
    );
}

fn record_mutation_diagnostic(
    #[cfg(debug_assertions)] diagnostics: &Arc<Mutex<Vec<MutationDiagnostic>>>,
    operation_id: Uuid,
    cause_code: &str,
    journal_error_code: Option<String>,
) {
    #[cfg(debug_assertions)]
    diagnostics
        .lock()
        .expect("manual SFTP mutation-diagnostic mutex poisoned")
        .push(MutationDiagnostic {
            operation_id,
            cause_code: cause_code.to_owned(),
            journal_error_code,
        });

    #[cfg(not(debug_assertions))]
    log::warn!(
        target: "harness_shell::manual_sftp",
        "manual_sftp_mutation_diagnostic operation_id={} cause_code={} journal_code={}",
        operation_id,
        cause_code,
        journal_error_code.as_deref().unwrap_or("none")
    );
}

async fn persist_operation_unknown(
    journal: &LocalSftpJournalActor,
    operation_id: Uuid,
) -> Result<(), String> {
    let mut record = journal
        .get(operation_id)
        .await
        .map_err(|error| error.code().to_owned())?
        .ok_or_else(|| "SFTP_JOURNAL_RECORD_MISSING".to_owned())?;
    record.state = OperationState::OutcomeUnknown;
    journal
        .put(record)
        .await
        .map_err(|error| error.code().to_owned())
}

impl CoordinatorShutdownOutcome {
    pub fn drained(self) -> bool {
        self.drained
    }
}

/// Arguments captured after the native picker returns an upload source.
///
/// This type is deliberately not serializable: the selected local path remains inside the Rust
/// Core boundary and is never sent to the WebView or Sidecar.
pub struct UploadPreparationInput {
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub local_path: PathBuf,
    pub remote_path: String,
    pub host_label: String,
}

/// Arguments captured after the native picker returns a download destination.
pub struct DownloadPreparationInput {
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub local_path: PathBuf,
    pub remote_path: String,
    pub host_label: String,
}

/// A mkdir is a mutation and must not run beside a prepared or active transfer.
pub struct MkdirInput {
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub parent_path: String,
    pub name: String,
    pub host_label: String,
}

/// Arguments for one explicitly confirmed atomic remote rename/move.
pub struct RenameInput {
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub source_path: String,
    pub target_path: String,
    pub overwrite: bool,
    pub host_label: String,
}

/// Arguments for removing one unchanged file, symlink, or empty directory.
pub struct RemoveInput {
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub path: String,
    pub host_label: String,
}

/// Arguments for the read-only recursive-delete manifest scan.
pub struct DeletePreflightInput {
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub path: String,
    pub host_label: String,
}

/// Public, non-sensitive one-shot preparation receipt.
#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransferPreparationReceipt {
    pub preparation_id: Uuid,
    pub operation_id: Uuid,
    pub direction: TransferDirection,
    pub display_name: String,
    pub remote_path: String,
    pub host_label: String,
    pub source_sha256: String,
    pub source_byte_count: u64,
    pub target_snapshot: TransferSnapshot,
    pub overwrite_required: bool,
    pub expires_at: String,
}

/// A cancellation flag and phase owned by the coordinator for one dispatched operation.
pub struct OperationControl {
    ssh_session_id: Uuid,
    cancel_requested: Arc<AtomicBool>,
    phase: OperationPhase,
    progress: TransferProgressProjection,
}

struct UploadPreparation {
    receipt: TransferPreparationReceipt,
    ssh_session_id: Uuid,
    connection_id: Uuid,
    source: FrozenUploadHandle,
    target_snapshot: TransferSnapshot,
    expires_at: Instant,
}

struct DownloadPreparation {
    receipt: TransferPreparationReceipt,
    ssh_session_id: Uuid,
    connection_id: Uuid,
    target: PreparedDownloadTarget,
    source_hash: String,
    source_byte_count: u64,
    source_snapshot: TransferSnapshot,
    expires_at: Instant,
}

enum TransferPreparation {
    Upload(UploadPreparation),
    Download(DownloadPreparation),
}

struct DeletePreparation {
    summary: DeletePlanSummary,
    connection_id: Uuid,
    host_label: String,
}

impl TransferPreparation {
    fn receipt(&self) -> &TransferPreparationReceipt {
        match self {
            Self::Upload(preparation) => &preparation.receipt,
            Self::Download(preparation) => &preparation.receipt,
        }
    }

    fn expires_at(&self) -> Instant {
        match self {
            Self::Upload(preparation) => preparation.expires_at,
            Self::Download(preparation) => preparation.expires_at,
        }
    }
}

/// Coordinates manual-SFTP state transitions without holding a mutex across local or Sidecar I/O.
///
/// Gate ownership is intentionally separate from the mutex guard lifetime. A preparation or active
/// transfer owns the two gates; every lock below protects only a small state transition.
pub struct SftpCoordinator {
    runtime: ManualSftpRuntimeClient,
    mutation_dispatch: MutationDispatchActor,
    journal: LocalSftpJournalActor,
    local_files: LocalFileActor,
    progress_sink: Arc<dyn TransferProgressSink>,
    transfer_owner: Mutex<Option<Uuid>>,
    mutation_owner: Mutex<Option<Uuid>>,
    preparations: Mutex<HashMap<Uuid, TransferPreparation>>,
    delete_preparations: Mutex<HashMap<Uuid, DeletePreparation>>,
    active_operations: Mutex<HashMap<Uuid, OperationControl>>,
    mutation_cancellations: Mutex<HashMap<Uuid, Arc<AtomicBool>>>,
    lifecycle: Mutex<CoordinatorLifecycle>,
    drain_notify: Arc<Notify>,
    detached_cleanups: Arc<DetachedCleanupTracker>,
    #[cfg(debug_assertions)]
    mutating_dispatch_test_gate: Option<MutatingDispatchTestGate>,
    #[cfg(debug_assertions)]
    mutation_diagnostics: Arc<Mutex<Vec<MutationDiagnostic>>>,
}

struct WorkflowGuard<'a> {
    coordinator: &'a SftpCoordinator,
}

impl Drop for WorkflowGuard<'_> {
    fn drop(&mut self) {
        self.coordinator.finish_workflow();
    }
}

/// Cancellation-safe owner for all in-memory resources held by one workflow. Rust drops this
/// guard when an async command future is aborted, so cleanup cannot depend on reaching a tail
/// `await`. File close is a non-waiting enqueue to the dedicated blocking owner.
struct OperationOwnerGuard<'a> {
    coordinator: &'a SftpCoordinator,
    operation_id: Uuid,
    owns_gates: bool,
    owns_active: bool,
    owns_mutation_registration: bool,
    upload_handle_id: Option<Uuid>,
    part_handle_id: Option<Uuid>,
    remote_cleanup_direction: Option<TransferDirection>,
}

impl<'a> OperationOwnerGuard<'a> {
    fn new(coordinator: &'a SftpCoordinator, operation_id: Uuid) -> Self {
        Self {
            coordinator,
            operation_id,
            owns_gates: true,
            owns_active: false,
            owns_mutation_registration: false,
            upload_handle_id: None,
            part_handle_id: None,
            remote_cleanup_direction: None,
        }
    }

    fn own_upload(&mut self, handle_id: Uuid) {
        self.upload_handle_id = Some(handle_id);
    }

    fn own_part(&mut self, handle_id: Uuid) {
        self.part_handle_id = Some(handle_id);
    }

    fn disown_part(&mut self) {
        self.part_handle_id = None;
    }

    fn arm_remote_cleanup(&mut self, direction: TransferDirection) {
        self.remote_cleanup_direction = Some(direction);
    }

    fn disarm_remote_cleanup(&mut self) {
        self.remote_cleanup_direction = None;
    }

    fn registered_mutation(&mut self) {
        self.owns_mutation_registration = true;
    }

    fn active(&mut self) {
        self.owns_active = true;
    }

    fn transfer_to_preparation(mut self) {
        self.owns_gates = false;
        self.upload_handle_id = None;
    }
}

impl Drop for OperationOwnerGuard<'_> {
    fn drop(&mut self) {
        if let Some(handle_id) = self.upload_handle_id.take() {
            self.coordinator.local_files.close_upload(handle_id);
        }
        if let Some(direction) = self.remote_cleanup_direction.take() {
            let part_handle_id = self.part_handle_id.take();
            let cleanup = self.coordinator.detached_cleanups.begin();
            if let Err(error) = self
                .coordinator
                .mutation_dispatch
                .enqueue_detached_transfer_abort(
                    self.operation_id,
                    direction,
                    part_handle_id,
                    cleanup,
                )
            {
                record_mutation_diagnostic(
                    #[cfg(debug_assertions)]
                    &self.coordinator.mutation_diagnostics,
                    self.operation_id,
                    "DETACHED_TRANSFER_ABORT_ENQUEUE_FAILED",
                    Some(error.code().to_owned()),
                );
            }
        } else if let Some(handle_id) = self.part_handle_id.take() {
            self.coordinator.local_files.close_part(handle_id);
        }
        if self.owns_active {
            self.coordinator.finish_active(self.operation_id);
        }
        if self.owns_mutation_registration {
            self.coordinator
                .unregister_mutation_cancellation(self.operation_id);
        }
        if self.owns_gates {
            self.coordinator.release_gates(self.operation_id);
        }
    }
}

/// Tauri-managed coordinator state.
pub struct SftpCoordinatorState {
    coordinator: Arc<SftpCoordinator>,
}

impl SftpCoordinatorState {
    pub fn new(coordinator: SftpCoordinator) -> Self {
        Self {
            coordinator: Arc::new(coordinator),
        }
    }

    pub fn coordinator(&self) -> Arc<SftpCoordinator> {
        Arc::clone(&self.coordinator)
    }

    /// Shutdown is ordered before the Sidecar supervisor. It only changes local ownership state;
    /// in-flight workflows observe their cancellation flag before another remote write.
    pub async fn shutdown(&self) -> CoordinatorShutdownOutcome {
        self.coordinator.shutdown().await
    }
}

impl SftpCoordinator {
    #[cfg(debug_assertions)]
    pub fn new(runtime: ManualSftpRuntimeClient, journal: LocalSftpOperationJournal) -> Self {
        Self::new_internal(
            runtime,
            journal,
            Arc::new(DiscardingTransferProgressSink),
            None,
            None,
            None,
            None,
        )
    }

    pub fn new_with_progress_sink(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpOperationJournal,
        progress_sink: Arc<dyn TransferProgressSink>,
    ) -> Self {
        #[cfg(debug_assertions)]
        {
            Self::new_internal(runtime, journal, progress_sink, None, None, None, None)
        }
        #[cfg(not(debug_assertions))]
        {
            Self::new_internal(runtime, journal, progress_sink)
        }
    }

    /// Validate an explicit terminal-tab session and return its safe SFTP context projection.
    pub async fn open_context(
        &self,
        ssh_session_id: Uuid,
    ) -> Result<super::models::ManualSftpContext, ManualSftpError> {
        self.runtime.open(ssh_session_id).await
    }

    /// Start one bounded remote directory listing for the explicitly selected session.
    pub async fn list_directory(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<super::models::ListingBatch, ManualSftpError> {
        self.runtime.list_begin(ssh_session_id, path).await
    }

    pub async fn next_directory_batch(
        &self,
        listing_id: Uuid,
        sequence: u32,
    ) -> Result<super::models::ListingBatch, ManualSftpError> {
        self.runtime.list_next(listing_id, sequence).await
    }

    pub async fn close_listing(&self, listing_id: Uuid) -> Result<bool, ManualSftpError> {
        self.runtime.list_close(listing_id).await
    }

    pub async fn inspect_entry(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<super::models::RemoteEntry, ManualSftpError> {
        let entry = self.runtime.lstat(ssh_session_id, path).await?;
        if entry.entry_type == super::models::EntryType::Symlink {
            return self.runtime.readlink(ssh_session_id, path).await;
        }
        Ok(entry)
    }

    pub async fn hash_file(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<super::models::RemoteFileHash, ManualSftpError> {
        self.runtime.sha256(ssh_session_id, path).await
    }

    /// Following a link is always explicit: first require a symlink via readlink, then resolve it.
    pub async fn open_link(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<super::models::RemoteEntry, ManualSftpError> {
        self.runtime.readlink(ssh_session_id, path).await?;
        self.runtime.realpath(ssh_session_id, path).await
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn new_with_mutating_dispatch_test_gate(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpOperationJournal,
        gate: MutatingDispatchTestGate,
    ) -> Self {
        Self::new_internal(
            runtime,
            journal,
            Arc::new(DiscardingTransferProgressSink),
            Some(gate),
            None,
            None,
            None,
        )
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn new_with_local_file_reply_test_gate(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpOperationJournal,
        gate: LocalFileReplyTestGate,
    ) -> Self {
        Self::new_internal(
            runtime,
            journal,
            Arc::new(DiscardingTransferProgressSink),
            None,
            Some(gate),
            None,
            None,
        )
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn new_with_journal_fault_test_gate(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpOperationJournal,
        fault: JournalFaultTestGate,
    ) -> Self {
        Self::new_internal(
            runtime,
            journal,
            Arc::new(DiscardingTransferProgressSink),
            None,
            None,
            None,
            Some(fault),
        )
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn new_with_dispatch_and_journal_test_gates(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpOperationJournal,
        dispatch_gate: MutatingDispatchTestGate,
        journal_fault: JournalFaultTestGate,
    ) -> Self {
        Self::new_internal(
            runtime,
            journal,
            Arc::new(DiscardingTransferProgressSink),
            Some(dispatch_gate),
            None,
            None,
            Some(journal_fault),
        )
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn new_with_progress_and_local_finish_test_gate(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpOperationJournal,
        progress_sink: Arc<dyn TransferProgressSink>,
        finish_gate: LocalFileFinishTestGate,
    ) -> Self {
        Self::new_internal(
            runtime,
            journal,
            progress_sink,
            None,
            None,
            Some(finish_gate),
            None,
        )
    }

    fn new_internal(
        runtime: ManualSftpRuntimeClient,
        journal: LocalSftpOperationJournal,
        progress_sink: Arc<dyn TransferProgressSink>,
        #[cfg(debug_assertions)] mutating_dispatch_test_gate: Option<MutatingDispatchTestGate>,
        #[cfg(debug_assertions)] local_file_reply_test_gate: Option<LocalFileReplyTestGate>,
        #[cfg(debug_assertions)] local_file_finish_test_gate: Option<LocalFileFinishTestGate>,
        #[cfg(debug_assertions)] journal_fault_test_gate: Option<JournalFaultTestGate>,
    ) -> Self {
        let journal = {
            #[cfg(debug_assertions)]
            {
                match journal_fault_test_gate {
                    Some(fault) => LocalSftpJournalActor::spawn_with_test_fault(journal, fault),
                    None => LocalSftpJournalActor::spawn(journal),
                }
            }
            #[cfg(not(debug_assertions))]
            {
                LocalSftpJournalActor::spawn(journal)
            }
        };
        let local_files = {
            #[cfg(debug_assertions)]
            {
                LocalFileActor::spawn(local_file_reply_test_gate, local_file_finish_test_gate)
            }
            #[cfg(not(debug_assertions))]
            {
                LocalFileActor::spawn()
            }
        };
        #[cfg(debug_assertions)]
        let mutation_diagnostics = Arc::new(Mutex::new(Vec::new()));
        let drain_notify = Arc::new(Notify::new());
        let detached_cleanups = Arc::new(DetachedCleanupTracker::new(Arc::clone(&drain_notify)));
        let mutation_dispatch = MutationDispatchActor::new(
            runtime.clone(),
            journal.clone(),
            local_files.clone(),
            #[cfg(debug_assertions)]
            Arc::clone(&mutation_diagnostics),
        );
        Self {
            runtime,
            mutation_dispatch,
            journal,
            local_files,
            progress_sink,
            transfer_owner: Mutex::new(None),
            mutation_owner: Mutex::new(None),
            preparations: Mutex::new(HashMap::new()),
            delete_preparations: Mutex::new(HashMap::new()),
            active_operations: Mutex::new(HashMap::new()),
            mutation_cancellations: Mutex::new(HashMap::new()),
            lifecycle: Mutex::new(CoordinatorLifecycle {
                state: CoordinatorLifecycleState::Open,
                active_workflows: 0,
            }),
            drain_notify,
            detached_cleanups,
            #[cfg(debug_assertions)]
            mutating_dispatch_test_gate,
            #[cfg(debug_assertions)]
            mutation_diagnostics,
        }
    }

    /// Freeze/hash the local source, ask the Sidecar for the remote preflight snapshot, then retain
    /// the resulting one-shot preparation and both gates for no more than five minutes.
    pub async fn prepare_upload(
        &self,
        input: UploadPreparationInput,
    ) -> Result<TransferPreparationReceipt, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        validate_path(&input.remote_path)?;
        let operation_id = Uuid::new_v4();
        self.acquire_transfer_and_mutation(operation_id)?;
        let mut owner = OperationOwnerGuard::new(self, operation_id);
        let source_handle_id = Uuid::new_v4();
        owner.own_upload(source_handle_id);

        // The file handle is the resource owner that prevents replacement while the user confirms.
        let source = match self
            .local_files
            .freeze_upload(source_handle_id, input.local_path.clone())
            .await
        {
            Ok(source) => source,
            Err(error) => return Err(error),
        };
        let target_snapshot = match self
            .runtime
            .upload_preflight(input.ssh_session_id, &input.remote_path)
            .await
        {
            Ok(snapshot) => snapshot,
            Err(error) => return Err(error),
        };

        let monotonic_expiry = Instant::now() + PREPARATION_TTL;
        let receipt = TransferPreparationReceipt {
            preparation_id: Uuid::new_v4(),
            operation_id,
            direction: TransferDirection::Upload,
            display_name: source.display_name.clone(),
            remote_path: input.remote_path,
            host_label: input.host_label,
            source_sha256: source.sha256.clone(),
            source_byte_count: source.byte_count,
            target_snapshot: target_snapshot.clone(),
            overwrite_required: target_snapshot.exists,
            expires_at: preparation_expiry_rfc3339()?,
        };
        let preparing = TransferProgressProjection {
            operation_id,
            direction: TransferDirection::Upload,
            phase: OperationPhase::Preparing,
            display_name: receipt.display_name.clone(),
            remote_path: receipt.remote_path.clone(),
            host_label: receipt.host_label.clone(),
            bytes_completed: 0,
            bytes_total: source.byte_count,
            cancellable: true,
        };
        self.insert_preparation_if_open(
            receipt.preparation_id,
            TransferPreparation::Upload(UploadPreparation {
                receipt: receipt.clone(),
                ssh_session_id: input.ssh_session_id,
                connection_id: input.connection_id,
                source,
                target_snapshot,
                expires_at: monotonic_expiry,
            }),
        )?;
        owner.transfer_to_preparation();
        self.emit_progress_projection(preparing);
        Ok(receipt)
    }

    /// Pre-hash the remote source before retaining the local target snapshot and both transfer gates.
    pub async fn prepare_download(
        &self,
        input: DownloadPreparationInput,
    ) -> Result<TransferPreparationReceipt, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        validate_path(&input.remote_path)?;
        let operation_id = Uuid::new_v4();
        self.acquire_transfer_and_mutation(operation_id)?;
        let owner = OperationOwnerGuard::new(self, operation_id);
        let remote = match self
            .runtime
            .sha256(input.ssh_session_id, &input.remote_path)
            .await
        {
            Ok(remote) => remote,
            Err(error) => return Err(error),
        };
        let target = match self
            .local_files
            .prepare_download_target(input.local_path.clone())
            .await
        {
            Ok(target) => target,
            Err(error) => return Err(error),
        };
        let safe_target_snapshot = TransferSnapshot {
            // A local absolute path never crosses the Core boundary. The canonical path and file
            // identity stay in `PreparedDownloadTarget`; the UI only receives its basename and
            // whether an explicit overwrite confirmation is required.
            path: target.display_name().to_owned(),
            exists: matches!(&target.snapshot, LocalTargetSnapshot::Existing(_)),
            entry_type: matches!(&target.snapshot, LocalTargetSnapshot::Existing(_))
                .then_some(EntryType::File),
            size: None,
            mtime_ns: None,
            sha256: None,
        };
        let monotonic_expiry = Instant::now() + PREPARATION_TTL;
        let receipt = TransferPreparationReceipt {
            preparation_id: Uuid::new_v4(),
            operation_id,
            direction: TransferDirection::Download,
            display_name: target.display_name().to_owned(),
            remote_path: input.remote_path,
            host_label: input.host_label,
            source_sha256: remote.sha256.clone(),
            source_byte_count: remote.byte_count,
            overwrite_required: safe_target_snapshot.exists,
            target_snapshot: safe_target_snapshot,
            expires_at: preparation_expiry_rfc3339()?,
        };
        let preparing = TransferProgressProjection {
            operation_id,
            direction: TransferDirection::Download,
            phase: OperationPhase::Preparing,
            display_name: receipt.display_name.clone(),
            remote_path: receipt.remote_path.clone(),
            host_label: receipt.host_label.clone(),
            bytes_completed: 0,
            bytes_total: remote.byte_count,
            cancellable: true,
        };
        self.insert_preparation_if_open(
            receipt.preparation_id,
            TransferPreparation::Download(DownloadPreparation {
                receipt: receipt.clone(),
                ssh_session_id: input.ssh_session_id,
                connection_id: input.connection_id,
                target,
                source_hash: remote.sha256,
                source_byte_count: remote.byte_count,
                source_snapshot: remote.snapshot,
                expires_at: monotonic_expiry,
            }),
        )?;
        owner.transfer_to_preparation();
        self.emit_progress_projection(preparing);
        Ok(receipt)
    }

    /// Discard consumes a preparation exactly once and releases only the gates it owns.
    pub async fn discard_preparation(&self, preparation_id: Uuid) -> Result<(), ManualSftpError> {
        let preparation = self.take_preparation(preparation_id)?;
        self.release_gates(preparation.receipt().operation_id);
        self.dispose_preparation(preparation);
        Ok(())
    }

    /// Execute an upload after an explicit UI confirmation. The preparation is removed before I/O,
    /// so a second consumer cannot execute or discard the same source.
    pub async fn execute_upload(
        &self,
        preparation_id: Uuid,
        confirmed: bool,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        if !confirmed {
            return Err(ManualSftpError::new(
                "SFTP_CONFIRMATION_REQUIRED",
                "The transfer requires explicit confirmation.",
            ));
        }
        let preparation = self.take_preparation(preparation_id)?;
        let TransferPreparation::Upload(preparation) = preparation else {
            self.release_gates_for_preparation(preparation);
            return Err(ManualSftpError::new(
                "SFTP_PREPARATION_KIND_INVALID",
                "The preparation does not describe an upload.",
            ));
        };
        self.execute_upload_preparation(preparation).await
    }

    /// Execute a download after an explicit UI confirmation.
    pub async fn execute_download(
        &self,
        preparation_id: Uuid,
        confirmed: bool,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        if !confirmed {
            return Err(ManualSftpError::new(
                "SFTP_CONFIRMATION_REQUIRED",
                "The transfer requires explicit confirmation.",
            ));
        }
        let preparation = self.take_preparation(preparation_id)?;
        let TransferPreparation::Download(preparation) = preparation else {
            self.release_gates_for_preparation(preparation);
            return Err(ManualSftpError::new(
                "SFTP_PREPARATION_KIND_INVALID",
                "The preparation does not describe a download.",
            ));
        };
        self.execute_download_preparation(preparation).await
    }

    /// A simple mutation uses only the Mutation Gate and persists before dispatch.
    pub async fn mkdir(
        &self,
        input: MkdirInput,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        validate_path(&input.parent_path)?;
        if input.name.is_empty() || input.name.contains('/') || input.name.contains('\\') {
            return Err(ManualSftpError::new(
                "SFTP_PATH_INVALID",
                "The remote directory name is invalid.",
            ));
        }
        let operation_id = Uuid::new_v4();
        self.acquire_mutation(operation_id)?;
        let mut owner = OperationOwnerGuard::new(self, operation_id);
        let record = LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::Mkdir,
            state: OperationState::Preparing,
            connection_id: input.connection_id,
            host_label: Some(input.host_label.clone()),
            local_path: None,
            remote_path: format!("{}/{}", input.parent_path.trim_end_matches('/'), input.name),
            expected_sha256: None,
            target_snapshot: None,
            created_at: OffsetDateTime::now_utc(),
        };
        self.register_mutation_cancellation(operation_id)?;
        owner.registered_mutation();
        self.put_journal(&record).await?;
        let dispatch = match self.enqueue_forward_mutation(
            operation_id,
            MutationDispatchRequest::Mkdir {
                operation_id,
                ssh_session_id: input.ssh_session_id,
                parent_path: input.parent_path,
                name: input.name,
            },
        ) {
            Ok(dispatch) => dispatch,
            Err(error) => {
                self.persist_terminal_then_delete(operation_id, OperationTerminalState::Cancelled)
                    .await?;
                return Err(error);
            }
        };
        let dispatch = start_mutation_dispatch(dispatch).await?;
        let terminal = with_mutation_timeout(async {
            terminal_dispatch_response(await_mutation_dispatch(dispatch).await?)
        })
        .await;
        self.finish_mutation(record, terminal).await
    }

    /// Atomically rename or move one remote entry without fallback.
    pub async fn rename(
        &self,
        input: RenameInput,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        validate_path(&input.source_path)?;
        validate_path(&input.target_path)?;
        // The WebView supplies only human intent. Rust freezes the remote identities before it
        // records or dispatches the mutation, leaving Python's immediate recheck authoritative.
        let source_snapshot = self
            .acquire_existing_remote_snapshot(input.ssh_session_id, &input.source_path)
            .await?;
        let target_metadata = self
            .acquire_rename_target_snapshot(input.ssh_session_id, &input.target_path)
            .await?;
        if target_metadata.exists && !input.overwrite {
            return Err(ManualSftpError::new(
                "SFTP_TARGET_EXISTS",
                "The remote rename target already exists.",
            ));
        }
        let target_snapshot = self
            .attach_regular_file_hash(input.ssh_session_id, &input.target_path, target_metadata)
            .await?;
        let operation_id = Uuid::new_v4();
        let record = LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::Rename,
            state: OperationState::Preparing,
            connection_id: input.connection_id,
            host_label: Some(input.host_label),
            local_path: None,
            remote_path: input.target_path.clone(),
            expected_sha256: None,
            target_snapshot: Some(target_snapshot.clone()),
            created_at: OffsetDateTime::now_utc(),
        };
        self.execute_simple_mutation(
            record,
            MutationDispatchRequest::Rename {
                operation_id,
                ssh_session_id: input.ssh_session_id,
                source_path: input.source_path,
                target_path: input.target_path,
                overwrite: input.overwrite,
                source_snapshot: Some(source_snapshot),
                target_snapshot: Some(target_snapshot),
            },
            MUTATING_REQUEST_TIMEOUT,
        )
        .await
    }

    /// Remove one entry only when its no-follow snapshot still matches.
    pub async fn remove(
        &self,
        input: RemoveInput,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        validate_path(&input.path)?;
        let expected_snapshot = self
            .acquire_existing_remote_snapshot(input.ssh_session_id, &input.path)
            .await?;
        let operation_id = Uuid::new_v4();
        let record = LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::Remove,
            state: OperationState::Preparing,
            connection_id: input.connection_id,
            host_label: Some(input.host_label),
            local_path: None,
            remote_path: input.path.clone(),
            expected_sha256: expected_snapshot.sha256.clone(),
            target_snapshot: Some(expected_snapshot.clone()),
            created_at: OffsetDateTime::now_utc(),
        };
        self.execute_simple_mutation(
            record,
            MutationDispatchRequest::Remove {
                operation_id,
                ssh_session_id: input.ssh_session_id,
                path: input.path,
                expected_snapshot,
            },
            MUTATING_REQUEST_TIMEOUT,
        )
        .await
    }

    async fn acquire_existing_remote_snapshot(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<TransferSnapshot, ManualSftpError> {
        let entry = self.runtime.lstat(ssh_session_id, path).await?;
        let snapshot = snapshot_from_lstat(path, &entry)?;
        self.attach_regular_file_hash(ssh_session_id, path, snapshot)
            .await
    }

    async fn acquire_rename_target_snapshot(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<TransferSnapshot, ManualSftpError> {
        // The target may be absent. This typed preflight returns an explicit absent no-follow
        // snapshot, avoiding any lstat-not-found compatibility path in the Rust coordinator.
        let snapshot = self.runtime.upload_preflight(ssh_session_id, path).await?;
        if snapshot.path != path {
            return Err(invalid_response(
                "The remote target snapshot did not describe the requested path.",
            ));
        }
        Ok(snapshot)
    }

    async fn attach_regular_file_hash(
        &self,
        ssh_session_id: Uuid,
        path: &str,
        snapshot: TransferSnapshot,
    ) -> Result<TransferSnapshot, ManualSftpError> {
        if !snapshot.exists || snapshot.entry_type != Some(EntryType::File) {
            return Ok(snapshot);
        }
        let hash = self.runtime.sha256(ssh_session_id, path).await?;
        verify_hashed_snapshot(path, &snapshot, &hash)?;
        Ok(hash.snapshot)
    }

    /// Build one complete no-follow manifest. This request performs no remote filesystem mutation,
    /// but it occupies the Mutation Gate while Python freezes the one-shot delete plan.
    pub async fn preflight_delete(
        &self,
        input: DeletePreflightInput,
    ) -> Result<DeletePlanSummary, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        validate_path(&input.path)?;
        let operation_id = Uuid::new_v4();
        self.acquire_mutation(operation_id)?;
        let mut owner = OperationOwnerGuard::new(self, operation_id);
        self.register_mutation_cancellation(operation_id)?;
        owner.registered_mutation();
        let mut record = LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::RecursiveDelete,
            state: OperationState::Preparing,
            connection_id: input.connection_id,
            host_label: Some(input.host_label.clone()),
            local_path: None,
            remote_path: input.path.clone(),
            expected_sha256: None,
            target_snapshot: None,
            created_at: OffsetDateTime::now_utc(),
        };
        // The caller-selected remote identity is durable before Python can persist a plan. A lost
        // reply can therefore be reconciled after restart without guessing or replaying preflight.
        self.put_journal(&record).await?;
        let dispatch = match self.enqueue_forward_mutation(
            operation_id,
            MutationDispatchRequest::DeletePreflight {
                operation_id,
                ssh_session_id: input.ssh_session_id,
                path: input.path.clone(),
            },
        ) {
            Ok(dispatch) => dispatch,
            Err(error) => {
                self.persist_terminal_then_delete(operation_id, OperationTerminalState::Cancelled)
                    .await?;
                return Err(error);
            }
        };
        let dispatch = start_mutation_dispatch(dispatch).await?;
        let response = match tokio::time::timeout(RECURSIVE_REQUEST_TIMEOUT, async {
            await_mutation_dispatch(dispatch).await
        })
        .await
        {
            Ok(Ok(response)) => response,
            Ok(Err(error)) => return Err(self.failure_after_dispatch(&mut record, error).await),
            Err(_) => {
                let error = ManualSftpError::new(
                    "SFTP_REQUEST_TIMEOUT",
                    "The recursive-delete preflight did not make progress before the deadline.",
                );
                return Err(self.unknown_after_dispatch(&mut record, error).await);
            }
        };
        let MutationDispatchResponse::DeletePlan(summary) = response else {
            return Err(invalid_response(
                "The recursive-delete preflight returned the wrong response type.",
            ));
        };
        if summary.root_path != input.path
            || summary.root_snapshot.path != summary.root_path
            || !summary.root_snapshot.exists
            || summary.root_snapshot.entry_type != Some(super::models::EntryType::Directory)
            || summary.operation_id != operation_id
            || summary.delete_plan_id == summary.operation_id
        {
            return Err(invalid_response(
                "The recursive-delete plan did not match the requested root.",
            ));
        }
        record.state = OperationState::Preparing;
        record.expected_sha256 = Some(summary.manifest_sha256.clone());
        record.target_snapshot = Some(summary.root_snapshot.clone());
        self.put_journal(&record).await?;
        let delete_plan_id = summary.delete_plan_id;
        // Publication shares the lifecycle lock with shutdown's Open -> Closing transition. If
        // shutdown wins, the late response is rejected; if publication wins, shutdown clears it.
        let lifecycle = self
            .lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned");
        let lifecycle_error = match lifecycle.state {
            CoordinatorLifecycleState::Open => None,
            CoordinatorLifecycleState::Closing => Some(coordinator_closing()),
            CoordinatorLifecycleState::Closed => Some(coordinator_closed()),
        };
        if let Some(error) = lifecycle_error {
            return Err(error);
        }
        let mut preparations = self
            .delete_preparations
            .lock()
            .expect("manual SFTP delete-preparation mutex poisoned");
        if preparations.contains_key(&delete_plan_id)
            || preparations
                .values()
                .any(|existing| existing.summary.operation_id == summary.operation_id)
        {
            return Err(invalid_response(
                "The recursive-delete plan identity was duplicated.",
            ));
        }
        preparations.insert(
            delete_plan_id,
            DeletePreparation {
                summary: summary.clone(),
                connection_id: input.connection_id,
                host_label: input.host_label,
            },
        );
        Ok(summary)
    }

    /// Consume one confirmed recursive-delete plan exactly once.
    pub async fn execute_delete(
        &self,
        delete_plan_id: Uuid,
        confirmed: bool,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        if !confirmed {
            return Err(ManualSftpError::new(
                "SFTP_CONFIRMATION_REQUIRED",
                "Recursive delete requires explicit confirmation.",
            ));
        }
        let (summary, connection_id, host_label) = self
            .delete_preparations
            .lock()
            .expect("manual SFTP delete-preparation mutex poisoned")
            .get(&delete_plan_id)
            .map(|preparation| {
                (
                    preparation.summary.clone(),
                    preparation.connection_id,
                    preparation.host_label.clone(),
                )
            })
            .ok_or_else(|| {
                ManualSftpError::new(
                    "SFTP_DELETE_PLAN_NOT_FOUND",
                    "The recursive-delete plan is no longer available.",
                )
            })?;
        let operation_id = summary.operation_id;
        self.acquire_mutation(operation_id)?;
        let mut owner = OperationOwnerGuard::new(self, operation_id);
        self.register_mutation_cancellation(operation_id)?;
        owner.registered_mutation();
        let record = LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::RecursiveDelete,
            state: OperationState::Preparing,
            connection_id,
            host_label: Some(host_label),
            local_path: None,
            remote_path: summary.root_path,
            expected_sha256: Some(summary.manifest_sha256),
            target_snapshot: Some(summary.root_snapshot),
            created_at: OffsetDateTime::now_utc(),
        };
        // Journal readiness and the Mutation Gate are established before the one-shot plan is
        // consumed. Busy or local persistence failures therefore leave a safe retry path.
        self.put_journal(&record).await?;
        let dispatch = self.enqueue_forward_mutation(
            operation_id,
            MutationDispatchRequest::DeleteExecute { delete_plan_id },
        )?;
        let dispatch = start_mutation_dispatch(dispatch).await?;
        self.delete_preparations
            .lock()
            .expect("manual SFTP delete-preparation mutex poisoned")
            .remove(&delete_plan_id);
        let terminal = with_mutation_timeout_duration(
            async { terminal_dispatch_response(await_mutation_dispatch(dispatch).await?) },
            RECURSIVE_REQUEST_TIMEOUT,
        )
        .await;
        self.finish_mutation(record, terminal).await
    }

    /// The cancellation request is idempotence-safe: a duplicate request is visible to the caller,
    /// while a committing operation is never interrupted after its irreversible remote transition.
    pub fn cancel(&self, operation_id: Uuid) -> Result<(), ManualSftpError> {
        let mut active = self
            .active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned");
        let control = active.get_mut(&operation_id).ok_or_else(|| {
            ManualSftpError::new("SFTP_OPERATION_NOT_ACTIVE", "The operation is not active.")
        })?;
        if control.phase == OperationPhase::Committing {
            return Err(ManualSftpError::new(
                "SFTP_CANCEL_TOO_LATE",
                "The operation is already committing.",
            ));
        }
        if control.cancel_requested.swap(true, Ordering::AcqRel) {
            return Err(ManualSftpError::new(
                "SFTP_CANCEL_ALREADY_REQUESTED",
                "Cancellation was already requested.",
            ));
        }
        Ok(())
    }

    /// Recovery discovery reads only DPAPI-protected local journal records and does not network.
    pub async fn list_recoveries(&self) -> Result<Vec<RecoverySummary>, ManualSftpError> {
        self.journal
            .list_non_terminal()
            .await
            .map_err(journal_error)
            .and_then(|records| records.into_iter().map(recovery_from_record).collect())
    }

    /// The caller must have explicitly connected the selected SSH session before this read-only
    /// Sidecar reconciliation request is invoked.
    pub async fn inspect_recovery(
        &self,
        recovery_id: Uuid,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        let record = self.recovery_record(recovery_id).await?;
        if record.kind == OperationKind::Download {
            return self
                .inspect_local_download_recovery(&record)
                .await
                .map(RecoveryResponse::Summary);
        }
        let remote_operation_id = record.remote_operation_id.unwrap_or(record.operation_id);
        let response = self.inspect_recovery_inner(remote_operation_id).await?;
        self.reconcile_recovery_response(recovery_id, remote_operation_id, response)
            .await
    }

    async fn inspect_recovery_inner(
        &self,
        recovery_id: Uuid,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        self.runtime.recovery_inspect(recovery_id).await
    }

    /// Recovery actions remain allowlisted and require a separate confirmation flag.
    ///
    /// A mutating recovery has a fresh local journal operation and Mutation Gate owner. Rust
    /// selects the fresh remote operation identity, persists the exact local-to-remote mapping,
    /// and trusts only a terminal receipt carrying that selected remote identity.
    pub async fn execute_recovery(
        &self,
        recovery_id: Uuid,
        action: RecoveryAction,
        confirmed: bool,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        let _workflow = self.begin_workflow()?;
        if !confirmed {
            return Err(ManualSftpError::new(
                "SFTP_CONFIRMATION_REQUIRED",
                "The recovery action requires explicit confirmation.",
            ));
        }
        let original = self.recovery_record(recovery_id).await?;
        if original.kind == OperationKind::Download {
            return self
                .execute_local_download_recovery(&original, action)
                .await
                .map(RecoveryResponse::Summary);
        }
        let original_remote_operation_id = original
            .remote_operation_id
            .unwrap_or(original.operation_id);
        let inspected = self
            .inspect_recovery_inner(original_remote_operation_id)
            .await?;
        let allowed = match self
            .reconcile_recovery_response(recovery_id, original_remote_operation_id, inspected)
            .await?
        {
            RecoveryResponse::Summary(summary) => summary,
            RecoveryResponse::Terminal(terminal) => {
                return Ok(RecoveryResponse::Terminal(terminal));
            }
        };
        if !allowed.available_actions.contains(&action) {
            return Err(ManualSftpError::new(
                "SFTP_RECOVERY_ACTION_NOT_ALLOWED",
                "The selected recovery action is not available.",
            ));
        }
        if !is_mutating_recovery_action(action) {
            let response = self
                .runtime
                .recovery_execute(original_remote_operation_id, action, Uuid::new_v4())
                .await?;
            return self
                .reconcile_recovery_response(recovery_id, original_remote_operation_id, response)
                .await;
        }

        let local_operation_id = Uuid::new_v4();
        let remote_operation_id = Uuid::new_v4();
        self.acquire_mutation(local_operation_id)?;
        let mut owner = OperationOwnerGuard::new(self, local_operation_id);
        let mut action_record = LocalSftpOperationRecord {
            operation_id: local_operation_id,
            remote_operation_id: Some(remote_operation_id),
            kind: OperationKind::Recovery,
            state: OperationState::Preparing,
            connection_id: original.connection_id,
            host_label: original.host_label,
            local_path: original.local_path,
            remote_path: original.remote_path,
            expected_sha256: original.expected_sha256,
            target_snapshot: original.target_snapshot,
            created_at: OffsetDateTime::now_utc(),
        };
        self.register_mutation_cancellation(local_operation_id)?;
        owner.registered_mutation();
        self.put_journal(&action_record).await?;

        let result = async {
            let dispatch = match self.enqueue_forward_mutation(
                local_operation_id,
                MutationDispatchRequest::RecoveryExecute {
                    recovery_id: original_remote_operation_id,
                    action,
                    operation_id: remote_operation_id,
                },
            ) {
                Ok(dispatch) => dispatch,
                Err(error) => {
                    self.persist_terminal_then_delete(
                        local_operation_id,
                        OperationTerminalState::Cancelled,
                    )
                    .await?;
                    return Err(error);
                }
            };
            let dispatch = start_mutation_dispatch(dispatch).await?;
            let result = match with_mutation_timeout(async {
                recovery_dispatch_response(await_mutation_dispatch(dispatch).await?)
            })
            .await
            {
                Ok(result) => result,
                Err(error) => {
                    return Err(self.failure_after_dispatch(&mut action_record, error).await)
                }
            };
            let RecoveryResponse::Terminal(terminal) = result else {
                return Err(self
                    .unknown_after_dispatch(
                        &mut action_record,
                        invalid_response("A mutating recovery did not return a terminal receipt."),
                    )
                    .await);
            };
            if terminal.operation_id != remote_operation_id {
                return Err(self
                    .unknown_after_dispatch(
                        &mut action_record,
                        invalid_response(
                            "The recovery response used an unexpected operation identity.",
                        ),
                    )
                    .await);
            }
            self.persist_terminal_then_delete(local_operation_id, terminal.state)
                .await?;
            if matches!(
                terminal.state,
                OperationTerminalState::Succeeded
                    | OperationTerminalState::Failed
                    | OperationTerminalState::Cancelled
            ) {
                self.persist_terminal_then_delete(recovery_id, OperationTerminalState::Failed)
                    .await?;
            }
            Ok(RecoveryResponse::Terminal(terminal))
        }
        .await;
        result
    }

    async fn inspect_local_download_recovery(
        &self,
        record: &LocalSftpOperationRecord,
    ) -> Result<RecoverySummary, ManualSftpError> {
        let target_path = record.local_path.clone().ok_or_else(journal_invalid)?;
        let expected_sha256 = record.expected_sha256.clone().ok_or_else(journal_invalid)?;
        let inspection = self
            .local_files
            .inspect_download_part(target_path, record.operation_id, expected_sha256)
            .await?;
        let summary = recovery_from_record(record.clone())?;
        if summary.display_name != inspection.display_name() {
            return Err(journal_invalid());
        }
        let _verified_byte_count = inspection.byte_count();
        Ok(summary)
    }

    async fn execute_local_download_recovery(
        &self,
        record: &LocalSftpOperationRecord,
        action: RecoveryAction,
    ) -> Result<RecoverySummary, ManualSftpError> {
        if !matches!(
            action,
            RecoveryAction::Verify | RecoveryAction::OpenLocalFolder | RecoveryAction::Keep
        ) {
            return Err(ManualSftpError::new(
                "SFTP_RECOVERY_ACTION_NOT_ALLOWED",
                "The selected recovery action is not available.",
            ));
        }
        if action != RecoveryAction::OpenLocalFolder {
            return self.inspect_local_download_recovery(record).await;
        }
        let target_path = record.local_path.clone().ok_or_else(journal_invalid)?;
        let expected_sha256 = record.expected_sha256.clone().ok_or_else(journal_invalid)?;
        let inspection = self
            .local_files
            .open_download_part_folder(target_path, record.operation_id, expected_sha256)
            .await?;
        let summary = recovery_from_record(record.clone())?;
        if summary.display_name != inspection.display_name() {
            return Err(journal_invalid());
        }
        Ok(summary)
    }

    /// Enter the irreversible closing state, request transfer cancellation, and wait a bounded
    /// interval for admitted workflows plus detached abort owners to settle local/remote/journal
    /// state.
    pub async fn shutdown(&self) -> CoordinatorShutdownOutcome {
        #[cfg(debug_assertions)]
        if let Some(gate) = &self.mutating_dispatch_test_gate {
            gate.mark_closing_attempted();
        }
        {
            let mut lifecycle = self
                .lifecycle
                .lock()
                .expect("manual SFTP lifecycle mutex poisoned");
            if lifecycle.state == CoordinatorLifecycleState::Closed {
                #[cfg(debug_assertions)]
                if let Some(gate) = &self.mutating_dispatch_test_gate {
                    gate.mark_closing_linearized();
                }
                return CoordinatorShutdownOutcome {
                    drained: lifecycle.active_workflows == 0
                        && self.detached_cleanups.active() == 0,
                };
            }
            lifecycle.state = CoordinatorLifecycleState::Closing;
            #[cfg(debug_assertions)]
            if let Some(gate) = &self.mutating_dispatch_test_gate {
                gate.mark_closing_linearized();
            }
        }

        {
            let cancellations = self
                .mutation_cancellations
                .lock()
                .expect("manual SFTP cancellation mutex poisoned");
            for cancellation in cancellations.values() {
                cancellation.store(true, Ordering::Release);
            }
        }

        let preparations = self
            .preparations
            .lock()
            .expect("manual SFTP preparation mutex poisoned")
            .drain()
            .map(|(_, preparation)| preparation)
            .collect::<Vec<_>>();
        for preparation in preparations {
            let operation_id = preparation.receipt().operation_id;
            self.release_gates(operation_id);
            self.dispose_preparation(preparation);
        }
        self.delete_preparations
            .lock()
            .expect("manual SFTP delete-preparation mutex poisoned")
            .clear();

        let deadline = Instant::now() + SHUTDOWN_DRAIN_TIMEOUT;
        let drained = loop {
            let notified = self.drain_notify.notified();
            let workflows_drained = self
                .lifecycle
                .lock()
                .expect("manual SFTP lifecycle mutex poisoned")
                .active_workflows
                == 0;
            if workflows_drained && self.detached_cleanups.active() == 0 {
                break true;
            }
            if tokio::time::timeout_at(deadline, notified).await.is_err() {
                break false;
            }
        };
        self.lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned")
            .state = CoordinatorLifecycleState::Closed;
        CoordinatorShutdownOutcome { drained }
    }

    pub fn gates_are_free(&self) -> bool {
        self.transfer_owner
            .lock()
            .expect("manual SFTP transfer-gate mutex poisoned")
            .is_none()
            && self
                .mutation_owner
                .lock()
                .expect("manual SFTP mutation-gate mutex poisoned")
                .is_none()
    }

    async fn execute_upload_preparation(
        &self,
        preparation: UploadPreparation,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let UploadPreparation {
            receipt,
            ssh_session_id,
            connection_id,
            source,
            target_snapshot,
            ..
        } = preparation;
        let operation_id = receipt.operation_id;
        let source_handle_id = source.handle_id;
        let source_path = source.path.clone();
        let source_sha256 = source.sha256.clone();
        let source_byte_count = source.byte_count;
        let mut owner = OperationOwnerGuard::new(self, operation_id);
        owner.own_upload(source_handle_id);
        let mut record = LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::Upload,
            state: OperationState::Transferring,
            connection_id,
            host_label: Some(receipt.host_label.clone()),
            local_path: Some(source_path),
            remote_path: receipt.remote_path.clone(),
            expected_sha256: Some(source_sha256.clone()),
            target_snapshot: Some(target_snapshot.clone()),
            created_at: OffsetDateTime::now_utc(),
        };
        let cancellation = self.register_mutation_cancellation(operation_id)?;
        owner.registered_mutation();
        self.begin_active(
            operation_id,
            ssh_session_id,
            TransferDirection::Upload,
            &receipt,
            source_byte_count,
            cancellation,
        );
        owner.active();
        self.put_journal(&record).await?;

        let result = async {
            let dispatch = match self.enqueue_forward_mutation(
                operation_id,
                MutationDispatchRequest::UploadBegin {
                    operation_id,
                    ssh_session_id,
                    path: receipt.remote_path.clone(),
                    source_sha256: source_sha256.clone(),
                    source_byte_count,
                    target_snapshot: target_snapshot.clone(),
                },
            ) {
                Ok(dispatch) => dispatch,
                Err(error) => {
                    self.persist_terminal_then_delete(
                        operation_id,
                        OperationTerminalState::Cancelled,
                    )
                    .await?;
                    return Err(error);
                }
            };
            let dispatch = start_mutation_dispatch(dispatch).await?;
            owner.arm_remote_cleanup(TransferDirection::Upload);
            let ready = with_mutation_timeout(async {
                upload_ready_dispatch_response(await_mutation_dispatch(dispatch).await?)
            })
            .await;
            let ready = match ready {
                Ok(ready) => ready,
                Err(error) => return Err(self.failure_after_dispatch(&mut record, error).await),
            };
            if ready.operation_id != operation_id
                || ready.next_sequence != 0
                || ready.next_offset != 0
            {
                return Err(self
                    .unknown_after_dispatch(
                        &mut record,
                        invalid_response("The upload begin response did not match the operation."),
                    )
                    .await);
            }
            let mut sequence = 0_u32;
            let mut offset = 0_u64;
            loop {
                if self.cancel_requested(operation_id) {
                    return self.abort_upload(operation_id, &mut record).await;
                }
                let chunk = match self
                    .local_files
                    .read_upload(source_handle_id, SFTP_CHUNK_BYTES)
                    .await
                {
                    Ok(chunk) => chunk,
                    Err(error) => {
                        return self
                            .abort_upload_after_error(operation_id, &mut record, error)
                            .await
                    }
                };
                if chunk.is_empty() {
                    break;
                }
                let chunk_length = chunk.len() as u64;
                let dispatch = match self.enqueue_forward_mutation(
                    operation_id,
                    MutationDispatchRequest::UploadChunk {
                        operation_id,
                        sequence,
                        offset,
                        chunk,
                    },
                ) {
                    Ok(dispatch) => dispatch,
                    Err(_) => return self.abort_upload(operation_id, &mut record).await,
                };
                let dispatch = match start_mutation_dispatch(dispatch).await {
                    Ok(dispatch) => dispatch,
                    Err(_) => return self.abort_upload(operation_id, &mut record).await,
                };
                let ack = with_mutation_timeout(async {
                    upload_chunk_dispatch_response(await_mutation_dispatch(dispatch).await?)
                })
                .await;
                let ack = match ack {
                    Ok(ack) => ack,
                    Err(error) => {
                        return Err(self.failure_after_dispatch(&mut record, error).await)
                    }
                };
                if ack.operation_id != operation_id
                    || ack.next_sequence != sequence + 1
                    || ack.next_offset != offset + chunk_length
                {
                    return Err(self
                        .unknown_after_dispatch(
                            &mut record,
                            invalid_response(
                                "The upload chunk acknowledgement did not match the chunk.",
                            ),
                        )
                        .await);
                }
                sequence += 1;
                offset += chunk_length;
                self.set_progress_bytes(operation_id, offset)?;
            }
            if offset != source_byte_count {
                return Err(self
                    .unknown_after_dispatch(
                        &mut record,
                        invalid_response("The frozen upload source ended at an unexpected offset."),
                    )
                    .await);
            }
            self.set_phase(operation_id, OperationPhase::Committing)?;
            record.state = OperationState::Committing;
            if let Err(error) = self.put_journal(&record).await {
                return self
                    .abort_upload_after_error(operation_id, &mut record, error)
                    .await;
            }
            let dispatch = match self.enqueue_forward_mutation(
                operation_id,
                MutationDispatchRequest::UploadFinish { operation_id },
            ) {
                Ok(dispatch) => dispatch,
                Err(_) => return self.abort_upload(operation_id, &mut record).await,
            };
            let dispatch = match start_mutation_dispatch(dispatch).await {
                Ok(dispatch) => dispatch,
                Err(_) => return self.abort_upload(operation_id, &mut record).await,
            };
            let terminal = match with_mutation_timeout(async {
                terminal_dispatch_response(await_mutation_dispatch(dispatch).await?)
            })
            .await
            {
                Ok(terminal) => terminal,
                Err(error) => return Err(self.failure_after_dispatch(&mut record, error).await),
            };
            if let Err(error) =
                self.require_upload_terminal(&terminal, operation_id, &source_sha256, offset)
            {
                return Err(self.unknown_after_dispatch(&mut record, error).await);
            }
            self.persist_terminal_then_delete(operation_id, terminal.state)
                .await?;
            Ok(terminal)
        }
        .await;
        owner.disarm_remote_cleanup();
        result
    }

    async fn execute_download_preparation(
        &self,
        preparation: DownloadPreparation,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let DownloadPreparation {
            receipt,
            ssh_session_id,
            connection_id,
            target,
            source_hash,
            source_byte_count,
            source_snapshot,
            ..
        } = preparation;
        let operation_id = receipt.operation_id;
        let target_path = target.path().to_path_buf();
        let mut owner = OperationOwnerGuard::new(self, operation_id);
        let mut record = LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::Download,
            state: OperationState::Transferring,
            connection_id,
            host_label: Some(receipt.host_label.clone()),
            local_path: Some(target_path),
            remote_path: receipt.remote_path.clone(),
            expected_sha256: Some(source_hash.clone()),
            target_snapshot: Some(source_snapshot.clone()),
            created_at: OffsetDateTime::now_utc(),
        };
        let cancellation = self.register_mutation_cancellation(operation_id)?;
        owner.registered_mutation();
        self.begin_active(
            operation_id,
            ssh_session_id,
            TransferDirection::Download,
            &receipt,
            source_byte_count,
            cancellation,
        );
        owner.active();
        self.put_journal(&record).await?;
        let result = async {
            let dispatch = match self.enqueue_forward_mutation(
                operation_id,
                MutationDispatchRequest::DownloadBegin {
                    operation_id,
                    ssh_session_id,
                    path: receipt.remote_path.clone(),
                },
            ) {
                Ok(dispatch) => dispatch,
                Err(error) => {
                    self.persist_terminal_then_delete(
                        operation_id,
                        OperationTerminalState::Cancelled,
                    )
                    .await?;
                    return Err(error);
                }
            };
            let dispatch = start_mutation_dispatch(dispatch).await?;
            owner.arm_remote_cleanup(TransferDirection::Download);
            let ready = with_mutation_timeout(async {
                download_ready_dispatch_response(await_mutation_dispatch(dispatch).await?)
            })
            .await;
            let ready = match ready {
                Ok(ready) => ready,
                Err(error) => return Err(self.failure_after_dispatch(&mut record, error).await),
            };
            if ready.operation_id != operation_id
                || ready.next_sequence != 0
                || ready.next_offset != 0
                || ready.sha256 != source_hash
                || ready.byte_count != source_byte_count
                || ready.snapshot != source_snapshot
            {
                return Err(self
                    .unknown_after_dispatch(
                        &mut record,
                        invalid_response(
                            "The download begin response did not match the preflight.",
                        ),
                    )
                    .await);
            }
            let part_handle_id = Uuid::new_v4();
            owner.own_part(part_handle_id);
            if let Err(error) = self
                .local_files
                .create_part(part_handle_id, target, operation_id)
                .await
            {
                owner.disown_part();
                return self
                    .abort_download_after_error(operation_id, None, &mut record, error)
                    .await;
            }
            let mut sequence = 0_u32;
            let mut offset = 0_u64;
            while source_byte_count > 0 {
                if self.cancel_requested(operation_id) {
                    return self
                        .abort_download(operation_id, part_handle_id, &mut record)
                        .await;
                }
                let dispatch = match self.enqueue_forward_mutation(
                    operation_id,
                    MutationDispatchRequest::DownloadChunk {
                        operation_id,
                        sequence,
                        offset,
                    },
                ) {
                    Ok(dispatch) => dispatch,
                    Err(_) => {
                        return self
                            .abort_download(operation_id, part_handle_id, &mut record)
                            .await
                    }
                };
                let dispatch = match start_mutation_dispatch(dispatch).await {
                    Ok(dispatch) => dispatch,
                    Err(_) => {
                        return self
                            .abort_download(operation_id, part_handle_id, &mut record)
                            .await
                    }
                };
                let chunk = with_mutation_timeout(async {
                    download_chunk_dispatch_response(await_mutation_dispatch(dispatch).await?)
                })
                .await;
                let chunk = match chunk {
                    Ok(chunk) => chunk,
                    Err(error) => {
                        return Err(self.failure_after_dispatch(&mut record, error).await)
                    }
                };
                if chunk.operation_id != operation_id
                    || chunk.sequence != sequence
                    || chunk.offset != offset
                {
                    return Err(self
                        .unknown_after_dispatch(
                            &mut record,
                            invalid_response(
                                "The download chunk did not match the requested range.",
                            ),
                        )
                        .await);
                }
                let bytes = chunk.bytes.to_vec();
                if chunk.next_offset != offset + bytes.len() as u64 {
                    return Err(self
                        .unknown_after_dispatch(
                            &mut record,
                            invalid_response("The download chunk length did not match its offset."),
                        )
                        .await);
                }
                if let Err(error) = self.local_files.write_part(part_handle_id, bytes).await {
                    return self
                        .abort_download_after_error(
                            operation_id,
                            Some(part_handle_id),
                            &mut record,
                            error,
                        )
                        .await;
                }
                offset = chunk.next_offset;
                sequence += 1;
                self.set_progress_bytes(operation_id, offset)?;
                if chunk.eof {
                    break;
                }
            }
            if offset != source_byte_count {
                return Err(self
                    .unknown_after_dispatch(
                        &mut record,
                        invalid_response("The downloaded byte count did not match the preflight."),
                    )
                    .await);
            }
            self.set_phase(operation_id, OperationPhase::Verifying)?;
            record.state = OperationState::Verifying;
            if let Err(error) = self.put_journal(&record).await {
                return self
                    .abort_download_after_error(
                        operation_id,
                        Some(part_handle_id),
                        &mut record,
                        error,
                    )
                    .await;
            }
            let dispatch = match self.enqueue_forward_mutation(
                operation_id,
                MutationDispatchRequest::DownloadFinish { operation_id },
            ) {
                Ok(dispatch) => dispatch,
                Err(_) => {
                    return self
                        .abort_download(operation_id, part_handle_id, &mut record)
                        .await
                }
            };
            let dispatch = match start_mutation_dispatch(dispatch).await {
                Ok(dispatch) => dispatch,
                Err(_) => {
                    return self
                        .abort_download(operation_id, part_handle_id, &mut record)
                        .await
                }
            };
            let terminal = match with_mutation_timeout(async {
                terminal_dispatch_response(await_mutation_dispatch(dispatch).await?)
            })
            .await
            {
                Ok(terminal) => terminal,
                Err(error) => return Err(self.failure_after_dispatch(&mut record, error).await),
            };
            if let Err(error) =
                self.require_download_terminal(&terminal, operation_id, &source_hash, offset)
            {
                return Err(self.unknown_after_dispatch(&mut record, error).await);
            }
            // A cancellation can arrive while the remote finish receipt is in flight. Recheck
            // before the irreversible local replace; once Committing is set, cancellation is too
            // late by contract.
            if self.cancel_requested(operation_id) {
                if let Err(error) = self.local_files.abort_part(part_handle_id).await {
                    record.state = OperationState::CleanupRequired;
                    self.put_journal(&record).await?;
                    return Err(error);
                }
                let mut cancelled = terminal;
                cancelled.state = OperationTerminalState::Cancelled;
                cancelled.error_code = None;
                cancelled.message = "The local download commit was cancelled.".to_owned();
                self.persist_terminal_then_delete(operation_id, cancelled.state)
                    .await?;
                return Ok(cancelled);
            }
            self.set_phase(operation_id, OperationPhase::Committing)?;
            record.state = OperationState::Committing;
            self.put_journal(&record).await?;
            if let Err(error) = self
                .local_files
                .finish_part(part_handle_id, source_hash)
                .await
            {
                record.state = OperationState::CleanupRequired;
                self.put_journal(&record).await?;
                return Err(error);
            }
            self.persist_terminal_then_delete(operation_id, terminal.state)
                .await?;
            Ok(terminal)
        }
        .await;
        owner.disarm_remote_cleanup();
        result
    }

    async fn abort_upload(
        &self,
        operation_id: Uuid,
        record: &mut LocalSftpOperationRecord,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let dispatch = self.enqueue_cleanup_mutation(
            operation_id,
            MutationDispatchRequest::UploadAbort { operation_id },
        )?;
        let dispatch = start_mutation_dispatch(dispatch).await?;
        let terminal = match with_mutation_timeout(async {
            terminal_dispatch_response(await_mutation_dispatch(dispatch).await?)
        })
        .await
        {
            Ok(terminal) => terminal,
            Err(error) => return Err(self.unknown_after_dispatch(record, error).await),
        };
        if terminal.operation_id != operation_id
            || terminal.state != OperationTerminalState::Cancelled
        {
            return Err(self
                .unknown_after_dispatch(
                    record,
                    invalid_response("The upload abort response could not be confirmed."),
                )
                .await);
        }
        self.persist_terminal_then_delete(operation_id, terminal.state)
            .await?;
        Ok(terminal)
    }

    async fn abort_upload_after_error(
        &self,
        operation_id: Uuid,
        record: &mut LocalSftpOperationRecord,
        original: ManualSftpError,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        match self.abort_upload(operation_id, record).await {
            Ok(_) => Err(original),
            Err(cleanup_error) => Err(cleanup_error),
        }
    }

    async fn abort_download(
        &self,
        operation_id: Uuid,
        part_handle_id: Uuid,
        record: &mut LocalSftpOperationRecord,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let dispatch = self.enqueue_cleanup_mutation(
            operation_id,
            MutationDispatchRequest::DownloadAbort { operation_id },
        )?;
        let dispatch = start_mutation_dispatch(dispatch).await?;
        let terminal = match with_mutation_timeout(async {
            terminal_dispatch_response(await_mutation_dispatch(dispatch).await?)
        })
        .await
        {
            Ok(terminal) => terminal,
            Err(error) => return Err(self.unknown_after_dispatch(record, error).await),
        };
        if terminal.operation_id != operation_id
            || terminal.state != OperationTerminalState::Cancelled
        {
            return Err(self
                .unknown_after_dispatch(
                    record,
                    invalid_response("The download abort response could not be confirmed."),
                )
                .await);
        }
        if let Err(error) = self.local_files.abort_part(part_handle_id).await {
            // The remote abort was confirmed, but the retained local part now needs an explicit
            // recovery action. Do not claim it was removed or downgrade the journal state.
            record.state = OperationState::CleanupRequired;
            self.put_journal(record).await?;
            return Err(error);
        }
        self.persist_terminal_then_delete(operation_id, terminal.state)
            .await?;
        Ok(terminal)
    }

    async fn abort_download_after_error(
        &self,
        operation_id: Uuid,
        part_handle_id: Option<Uuid>,
        record: &mut LocalSftpOperationRecord,
        original: ManualSftpError,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let cleanup = match part_handle_id {
            Some(part_handle_id) => {
                self.abort_download(operation_id, part_handle_id, record)
                    .await
            }
            None => {
                let dispatch = self.enqueue_cleanup_mutation(
                    operation_id,
                    MutationDispatchRequest::DownloadAbort { operation_id },
                )?;
                let dispatch = start_mutation_dispatch(dispatch).await?;
                let terminal = match with_mutation_timeout(async {
                    terminal_dispatch_response(await_mutation_dispatch(dispatch).await?)
                })
                .await
                {
                    Ok(terminal) => terminal,
                    Err(error) => {
                        return Err(self.unknown_after_dispatch(record, error).await);
                    }
                };
                if terminal.operation_id != operation_id
                    || terminal.state != OperationTerminalState::Cancelled
                {
                    return Err(self
                        .unknown_after_dispatch(
                            record,
                            invalid_response("The download abort response could not be confirmed."),
                        )
                        .await);
                }
                self.persist_terminal_then_delete(operation_id, terminal.state)
                    .await?;
                Ok(terminal)
            }
        };
        match cleanup {
            Ok(_) => Err(original),
            Err(cleanup_error) => Err(cleanup_error),
        }
    }

    async fn finish_mutation(
        &self,
        mut record: LocalSftpOperationRecord,
        terminal: Result<OperationTerminalProjection, ManualSftpError>,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let terminal = match terminal {
            Ok(terminal) if terminal.operation_id == record.operation_id => terminal,
            Ok(_) => {
                return Err(self
                    .unknown_after_dispatch(
                        &mut record,
                        invalid_response("The mutation receipt did not match the operation."),
                    )
                    .await)
            }
            Err(error) => return Err(self.failure_after_dispatch(&mut record, error).await),
        };
        match terminal.state {
            OperationTerminalState::Succeeded
            | OperationTerminalState::Failed
            | OperationTerminalState::Cancelled => {
                // A trustworthy terminal receipt is itself durable before deletion. If deletion
                // fails, restart observes the terminal record instead of stale `preparing` state.
                record.state = state_from_terminal(terminal.state);
                self.put_journal(&record).await?;
                self.journal_delete(record.operation_id).await?;
            }
            OperationTerminalState::CleanupRequired | OperationTerminalState::OutcomeUnknown => {
                record.state = state_from_terminal(terminal.state);
                self.put_journal(&record).await?;
            }
        }
        Ok(terminal)
    }

    async fn execute_simple_mutation(
        &self,
        record: LocalSftpOperationRecord,
        request: MutationDispatchRequest,
        timeout: Duration,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        let operation_id = record.operation_id;
        self.acquire_mutation(operation_id)?;
        let mut owner = OperationOwnerGuard::new(self, operation_id);
        self.register_mutation_cancellation(operation_id)?;
        owner.registered_mutation();
        self.put_journal(&record).await?;
        let dispatch = match self.enqueue_forward_mutation(operation_id, request) {
            Ok(dispatch) => dispatch,
            Err(error) => {
                self.persist_terminal_then_delete(operation_id, OperationTerminalState::Cancelled)
                    .await?;
                return Err(error);
            }
        };
        let dispatch = start_mutation_dispatch(dispatch).await?;
        let terminal = with_mutation_timeout_duration(
            async { terminal_dispatch_response(await_mutation_dispatch(dispatch).await?) },
            timeout,
        )
        .await;
        self.finish_mutation(record, terminal).await
    }

    fn begin_workflow(&self) -> Result<WorkflowGuard<'_>, ManualSftpError> {
        let mut lifecycle = self
            .lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned");
        match lifecycle.state {
            CoordinatorLifecycleState::Open => {
                lifecycle.active_workflows += 1;
                Ok(WorkflowGuard { coordinator: self })
            }
            CoordinatorLifecycleState::Closing => Err(coordinator_closing()),
            CoordinatorLifecycleState::Closed => Err(coordinator_closed()),
        }
    }

    fn finish_workflow(&self) {
        let mut lifecycle = self
            .lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned");
        lifecycle.active_workflows = lifecycle
            .active_workflows
            .checked_sub(1)
            .expect("manual SFTP active-workflow count underflow");
        let drained = lifecycle.active_workflows == 0;
        drop(lifecycle);
        if drained {
            self.drain_notify.notify_waiters();
        }
    }

    fn insert_preparation_if_open(
        &self,
        preparation_id: Uuid,
        preparation: TransferPreparation,
    ) -> Result<(), ManualSftpError> {
        let lifecycle = self
            .lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned");
        match lifecycle.state {
            CoordinatorLifecycleState::Open => {
                self.preparations
                    .lock()
                    .expect("manual SFTP preparation mutex poisoned")
                    .insert(preparation_id, preparation);
                return Ok(());
            }
            CoordinatorLifecycleState::Closing => Err(coordinator_closing()),
            CoordinatorLifecycleState::Closed => Err(coordinator_closed()),
        }
    }

    fn take_preparation(
        &self,
        preparation_id: Uuid,
    ) -> Result<TransferPreparation, ManualSftpError> {
        let preparation = self
            .preparations
            .lock()
            .expect("manual SFTP preparation mutex poisoned")
            .remove(&preparation_id)
            .ok_or_else(|| {
                ManualSftpError::new(
                    "SFTP_PREPARATION_NOT_FOUND",
                    "The transfer preparation is no longer available.",
                )
            })?;
        if Instant::now() >= preparation.expires_at() {
            self.release_gates(preparation.receipt().operation_id);
            self.dispose_preparation(preparation);
            return Err(ManualSftpError::new(
                "SFTP_PREPARATION_EXPIRED",
                "The transfer preparation expired before execution.",
            ));
        }
        Ok(preparation)
    }

    fn expire_preparations(&self) {
        let expired = {
            let mut preparations = self
                .preparations
                .lock()
                .expect("manual SFTP preparation mutex poisoned");
            let now = Instant::now();
            let ids = preparations
                .iter()
                .filter_map(|(id, preparation)| (now >= preparation.expires_at()).then_some(*id))
                .collect::<Vec<_>>();
            ids.into_iter()
                .filter_map(|id| preparations.remove(&id))
                .collect::<Vec<_>>()
        };
        for preparation in expired {
            let operation_id = preparation.receipt().operation_id;
            self.release_gates(operation_id);
            self.dispose_preparation(preparation);
        }
    }

    fn acquire_transfer_and_mutation(&self, operation_id: Uuid) -> Result<(), ManualSftpError> {
        self.expire_preparations();
        let mut transfer = self
            .transfer_owner
            .lock()
            .expect("manual SFTP transfer-gate mutex poisoned");
        if transfer.is_some() {
            return Err(ManualSftpError::new(
                "SFTP_TRANSFER_BUSY",
                "Another transfer is already active.",
            ));
        }
        let mut mutation = self
            .mutation_owner
            .lock()
            .expect("manual SFTP mutation-gate mutex poisoned");
        if mutation.is_some() {
            return Err(ManualSftpError::new(
                "SFTP_MUTATION_BUSY",
                "Another mutation is already active.",
            ));
        }
        *transfer = Some(operation_id);
        *mutation = Some(operation_id);
        Ok(())
    }

    fn acquire_mutation(&self, operation_id: Uuid) -> Result<(), ManualSftpError> {
        self.expire_preparations();
        let mut mutation = self
            .mutation_owner
            .lock()
            .expect("manual SFTP mutation-gate mutex poisoned");
        if mutation.is_some() {
            return Err(ManualSftpError::new(
                "SFTP_MUTATION_BUSY",
                "Another mutation is already active.",
            ));
        }
        *mutation = Some(operation_id);
        Ok(())
    }

    fn release_gates_for_preparation(&self, preparation: TransferPreparation) {
        self.release_gates(preparation.receipt().operation_id);
        self.dispose_preparation(preparation);
    }

    fn dispose_preparation(&self, preparation: TransferPreparation) {
        if let TransferPreparation::Upload(preparation) = preparation {
            self.local_files.close_upload(preparation.source.handle_id);
        }
    }

    fn release_gates(&self, operation_id: Uuid) {
        let mut transfer = self
            .transfer_owner
            .lock()
            .expect("manual SFTP transfer-gate mutex poisoned");
        if *transfer == Some(operation_id) {
            *transfer = None;
        }
        drop(transfer);
        let mut mutation = self
            .mutation_owner
            .lock()
            .expect("manual SFTP mutation-gate mutex poisoned");
        if *mutation == Some(operation_id) {
            *mutation = None;
        }
    }

    /// Register cancellation ownership while holding the lifecycle lock. Once Closing wins this
    /// lock, no accepted workflow can appear later without already observing cancellation.
    fn register_mutation_cancellation(
        &self,
        operation_id: Uuid,
    ) -> Result<Arc<AtomicBool>, ManualSftpError> {
        let lifecycle = self
            .lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned");
        let error = match lifecycle.state {
            CoordinatorLifecycleState::Open => {
                let cancellation = Arc::new(AtomicBool::new(false));
                self.mutation_cancellations
                    .lock()
                    .expect("manual SFTP cancellation mutex poisoned")
                    .insert(operation_id, Arc::clone(&cancellation));
                return Ok(cancellation);
            }
            CoordinatorLifecycleState::Closing => coordinator_closing(),
            CoordinatorLifecycleState::Closed => coordinator_closed(),
        };
        drop(lifecycle);
        Err(error)
    }

    fn unregister_mutation_cancellation(&self, operation_id: Uuid) {
        self.mutation_cancellations
            .lock()
            .expect("manual SFTP cancellation mutex poisoned")
            .remove(&operation_id);
    }

    /// Linearization point for a forward mutation: lifecycle/cancellation check and the
    /// non-waiting single-owner enqueue happen while the same in-memory mutex is held. No mutex is
    /// retained while waiting for the actor or Sidecar response.
    fn enqueue_forward_mutation(
        &self,
        operation_id: Uuid,
        request: MutationDispatchRequest,
    ) -> Result<MutationDispatchHandle, ManualSftpError> {
        let lifecycle = self
            .lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned");
        if lifecycle.state != CoordinatorLifecycleState::Open {
            return Err(operation_cancelled());
        }
        let allowed = self
            .mutation_cancellations
            .lock()
            .expect("manual SFTP cancellation mutex poisoned")
            .get(&operation_id)
            .map(|cancellation| !cancellation.load(Ordering::Acquire))
            .unwrap_or(false);
        if !allowed {
            return Err(operation_cancelled());
        }
        #[cfg(debug_assertions)]
        if let Some(gate) = &self.mutating_dispatch_test_gate {
            gate.block_after_check();
        }
        self.mutation_dispatch.enqueue(request, Some(operation_id))
    }

    /// Abort is a cleanup mutation and remains admissible after Closing, but it shares the same
    /// actor enqueue ordering so no direct mutating runtime call bypasses the owner.
    fn enqueue_cleanup_mutation(
        &self,
        _operation_id: Uuid,
        request: MutationDispatchRequest,
    ) -> Result<MutationDispatchHandle, ManualSftpError> {
        let _lifecycle = self
            .lifecycle
            .lock()
            .expect("manual SFTP lifecycle mutex poisoned");
        self.mutation_dispatch.enqueue(request, None)
    }

    fn begin_active(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        direction: TransferDirection,
        receipt: &TransferPreparationReceipt,
        bytes_total: u64,
        cancel_requested: Arc<AtomicBool>,
    ) {
        let progress = TransferProgressProjection {
            operation_id,
            direction,
            phase: OperationPhase::Transferring,
            display_name: receipt.display_name.clone(),
            remote_path: receipt.remote_path.clone(),
            host_label: receipt.host_label.clone(),
            bytes_completed: 0,
            bytes_total,
            cancellable: true,
        };
        self.active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned")
            .insert(
                operation_id,
                OperationControl {
                    ssh_session_id,
                    cancel_requested,
                    phase: OperationPhase::Transferring,
                    progress: progress.clone(),
                },
            );
        self.emit_progress_projection(progress);
    }

    fn finish_active(&self, operation_id: Uuid) {
        self.active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned")
            .remove(&operation_id);
    }

    fn set_phase(&self, operation_id: Uuid, phase: OperationPhase) -> Result<(), ManualSftpError> {
        let mut active = self
            .active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned");
        let control = active.get_mut(&operation_id).ok_or_else(|| {
            ManualSftpError::new("SFTP_OPERATION_NOT_ACTIVE", "The operation is not active.")
        })?;
        control.phase = phase;
        control.progress.phase = phase;
        control.progress.cancellable = phase != OperationPhase::Committing;
        let projection = control.progress.clone();
        drop(active);
        self.emit_progress_projection(projection);
        Ok(())
    }

    fn set_progress_bytes(
        &self,
        operation_id: Uuid,
        bytes_completed: u64,
    ) -> Result<(), ManualSftpError> {
        let mut active = self
            .active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned");
        let control = active.get_mut(&operation_id).ok_or_else(|| {
            ManualSftpError::new("SFTP_OPERATION_NOT_ACTIVE", "The operation is not active.")
        })?;
        if bytes_completed > control.progress.bytes_total {
            return Err(invalid_response(
                "Transfer progress exceeded the prepared byte count.",
            ));
        }
        control.progress.bytes_completed = bytes_completed;
        let projection = control.progress.clone();
        drop(active);
        self.emit_progress_projection(projection);
        Ok(())
    }

    fn emit_progress_projection(&self, projection: TransferProgressProjection) {
        if let Err(error) = self.progress_sink.emit(projection.clone()) {
            // UI event transport is observational. Failing the remote operation here could leave
            // an already-dispatched mutation orphaned, so retain the coordinator state and emit a
            // bounded, path-free diagnostic instead of retrying or changing transfer semantics.
            log::warn!(
                target: "harness_shell::sftp",
                "manual SFTP transfer progress emission failed: operation_id={} phase={:?} code={}",
                projection.operation_id,
                projection.phase,
                error.code()
            );
        }
    }

    /// Returns the only serializable transfer-progress shape available to the WebView layer.
    pub fn progress(&self, operation_id: Uuid) -> Option<TransferProgressProjection> {
        self.active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned")
            .get(&operation_id)
            .map(|control| control.progress.clone())
    }

    /// Return the single active transfer only when it belongs to the requested SSH session.
    ///
    /// This is the defense-in-depth source used by connection teardown; it exposes the same safe
    /// projection as the progress event and never includes local file paths.
    pub fn active_transfer_for_session(
        &self,
        ssh_session_id: Uuid,
    ) -> Option<TransferProgressProjection> {
        self.active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned")
            .values()
            .find(|control| control.ssh_session_id == ssh_session_id)
            .map(|control| control.progress.clone())
    }

    /// Return the globally active transfer for application-exit protection.
    pub fn active_transfer(&self) -> Option<TransferProgressProjection> {
        self.active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned")
            .values()
            .next()
            .map(|control| control.progress.clone())
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub async fn local_file_owner_count_for_test(&self) -> Result<usize, ManualSftpError> {
        self.local_files.owner_count().await
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn mutation_registration_count_for_test(&self) -> usize {
        self.mutation_cancellations
            .lock()
            .expect("manual SFTP cancellation mutex poisoned")
            .len()
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn delete_preparation_count_for_test(&self) -> usize {
        self.delete_preparations
            .lock()
            .expect("manual SFTP delete-preparation mutex poisoned")
            .len()
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn mutation_diagnostics_for_test(&self) -> Vec<MutationDiagnostic> {
        self.mutation_diagnostics
            .lock()
            .expect("manual SFTP mutation-diagnostic mutex poisoned")
            .clone()
    }

    fn cancel_requested(&self, operation_id: Uuid) -> bool {
        self.active_operations
            .lock()
            .expect("manual SFTP active-operation mutex poisoned")
            .get(&operation_id)
            .map(|control| control.cancel_requested.load(Ordering::Acquire))
            .unwrap_or(false)
    }

    fn require_upload_terminal(
        &self,
        terminal: &OperationTerminalProjection,
        operation_id: Uuid,
        sha256: &str,
        byte_count: u64,
    ) -> Result<(), ManualSftpError> {
        if terminal.operation_id != operation_id
            || terminal.state != OperationTerminalState::Succeeded
            || terminal.sha256.as_deref() != Some(sha256)
            || terminal.byte_count != Some(byte_count)
        {
            return Err(invalid_response(
                "The upload finish receipt did not match the source.",
            ));
        }
        Ok(())
    }

    fn require_download_terminal(
        &self,
        terminal: &OperationTerminalProjection,
        operation_id: Uuid,
        sha256: &str,
        byte_count: u64,
    ) -> Result<(), ManualSftpError> {
        self.require_upload_terminal(terminal, operation_id, sha256, byte_count)
    }

    async fn put_journal(&self, record: &LocalSftpOperationRecord) -> Result<(), ManualSftpError> {
        self.journal
            .put(record.clone())
            .await
            .map_err(journal_error)
    }

    async fn journal_delete(&self, operation_id: Uuid) -> Result<(), ManualSftpError> {
        let deleted = self
            .journal
            .delete(operation_id)
            .await
            .map_err(journal_error)?;
        if !deleted {
            return Err(ManualSftpError::new(
                "SFTP_JOURNAL_INVARIANT",
                "The local operation record disappeared before its expected deletion.",
            ));
        }
        Ok(())
    }

    async fn persist_terminal_then_delete(
        &self,
        operation_id: Uuid,
        terminal: OperationTerminalState,
    ) -> Result<(), ManualSftpError> {
        let mut record = self
            .journal
            .get(operation_id)
            .await
            .map_err(journal_error)?
            .ok_or_else(|| {
                ManualSftpError::new(
                    "SFTP_RECOVERY_NOT_FOUND",
                    "The local recovery record is unavailable.",
                )
            })?;
        record.state = state_from_terminal(terminal);
        self.put_journal(&record).await?;
        if matches!(
            terminal,
            OperationTerminalState::Succeeded
                | OperationTerminalState::Failed
                | OperationTerminalState::Cancelled
        ) {
            self.journal_delete(operation_id).await?;
        }
        Ok(())
    }

    async fn reconcile_recovery_response(
        &self,
        local_recovery_id: Uuid,
        remote_operation_id: Uuid,
        mut response: RecoveryResponse,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        match &mut response {
            RecoveryResponse::Summary(summary)
                if summary.recovery_id == remote_operation_id
                    && summary.operation_id == remote_operation_id =>
            {
                summary.recovery_id = local_recovery_id;
            }
            RecoveryResponse::Terminal(terminal)
                if terminal.operation_id == remote_operation_id =>
            {
                self.persist_terminal_then_delete(local_recovery_id, terminal.state)
                    .await?;
            }
            _ => {
                return Err(invalid_response(
                    "The recovery response did not match the requested recovery identity.",
                ))
            }
        }
        Ok(response)
    }

    async fn recovery_record(
        &self,
        local_recovery_id: Uuid,
    ) -> Result<LocalSftpOperationRecord, ManualSftpError> {
        self.journal
            .get(local_recovery_id)
            .await
            .map_err(journal_error)?
            .ok_or_else(|| {
                ManualSftpError::new(
                    "SFTP_RECOVERY_NOT_FOUND",
                    "The local recovery record is no longer available.",
                )
            })
    }

    async fn failure_after_dispatch(
        &self,
        record: &mut LocalSftpOperationRecord,
        cause: ManualSftpError,
    ) -> ManualSftpError {
        if !cause.is_trusted_remote() {
            return self.unknown_after_dispatch(record, cause).await;
        }
        if let Some(retained_state) = cause.retained_operation_state() {
            record.state = match retained_state {
                super::models::RetainedOperationState::CleanupRequired => {
                    OperationState::CleanupRequired
                }
                super::models::RetainedOperationState::OutcomeUnknown => {
                    OperationState::OutcomeUnknown
                }
            };
            if let Err(error) = self.put_journal(record).await {
                return error;
            }
            return cause;
        }
        record.state = OperationState::Failed;
        if let Err(error) = self.put_journal(record).await {
            return error;
        }
        if let Err(error) = self.journal_delete(record.operation_id).await {
            return error;
        }
        cause
    }

    async fn unknown_after_dispatch(
        &self,
        record: &mut LocalSftpOperationRecord,
        cause: ManualSftpError,
    ) -> ManualSftpError {
        record.state = OperationState::OutcomeUnknown;
        // The public result is intentionally stable after dispatch. Broker/parser/journal details
        // remain internal because none of them proves whether the remote mutation took effect.
        let journal_error_code = self
            .put_journal(record)
            .await
            .err()
            .map(|error| error.code().to_owned());
        record_mutation_diagnostic(
            #[cfg(debug_assertions)]
            &self.mutation_diagnostics,
            record.operation_id,
            cause.code(),
            journal_error_code,
        );
        mutation_outcome_unknown()
    }
}

fn validate_path(path: &str) -> Result<(), ManualSftpError> {
    if path.is_empty() || !path.starts_with('/') || path.contains('\0') {
        return Err(ManualSftpError::new(
            "SFTP_PATH_INVALID",
            "The remote path is invalid.",
        ));
    }
    Ok(())
}

fn snapshot_from_lstat(
    requested_path: &str,
    entry: &RemoteEntry,
) -> Result<TransferSnapshot, ManualSftpError> {
    if entry.path != requested_path {
        return Err(invalid_response(
            "The remote metadata did not describe the requested path.",
        ));
    }
    Ok(TransferSnapshot {
        path: entry.path.clone(),
        exists: true,
        entry_type: Some(entry.entry_type),
        size: entry.size,
        mtime_ns: entry.mtime_ns.clone(),
        sha256: None,
    })
}

fn verify_hashed_snapshot(
    requested_path: &str,
    metadata: &TransferSnapshot,
    hash: &RemoteFileHash,
) -> Result<(), ManualSftpError> {
    let mut hashed_metadata = hash.snapshot.clone();
    let claimed_hash = hashed_metadata.sha256.take();
    // Python upload_preflight returns a regular-file snapshot with its hash, while this
    // Rust-private follow-up hash response carries that same hash as a separate proof.
    // Compare only immutable metadata after stripping the proof from both representations;
    // the checks below still require the response proof to match exactly.
    let mut expected_metadata = metadata.clone();
    let preflight_hash = expected_metadata.sha256.take();
    if hash.path != requested_path
        || hash.snapshot.path != requested_path
        || claimed_hash.as_deref() != Some(hash.sha256.as_str())
        || preflight_hash
            .as_deref()
            .is_some_and(|value| value != hash.sha256)
        || hash.byte_count != metadata.size.unwrap_or_default()
        || hashed_metadata != expected_metadata
    {
        return Err(invalid_response(
            "The remote file hash did not match the acquired metadata.",
        ));
    }
    Ok(())
}

async fn with_mutation_timeout<T>(
    request: impl Future<Output = Result<T, ManualSftpError>>,
) -> Result<T, ManualSftpError> {
    with_mutation_timeout_duration(request, MUTATING_REQUEST_TIMEOUT).await
}

async fn with_mutation_timeout_duration<T>(
    request: impl Future<Output = Result<T, ManualSftpError>>,
    timeout: Duration,
) -> Result<T, ManualSftpError> {
    tokio::time::timeout(timeout, request)
        .await
        .map_err(|_| mutation_outcome_unknown())?
}

async fn await_mutation_dispatch(
    response: oneshot::Receiver<Result<MutationDispatchResponse, ManualSftpError>>,
) -> Result<MutationDispatchResponse, ManualSftpError> {
    response
        .await
        .map_err(|_| mutation_dispatch_worker_failed())?
}

async fn start_mutation_dispatch(
    dispatch: MutationDispatchHandle,
) -> Result<oneshot::Receiver<Result<MutationDispatchResponse, ManualSftpError>>, ManualSftpError> {
    dispatch
        .started
        .await
        .map_err(|_| mutation_dispatch_worker_failed())??;
    Ok(dispatch.response)
}

fn terminal_dispatch_response(
    response: MutationDispatchResponse,
) -> Result<OperationTerminalProjection, ManualSftpError> {
    match response {
        MutationDispatchResponse::Terminal(terminal) => Ok(terminal),
        _ => Err(invalid_response(
            "The mutation dispatcher returned the wrong response type.",
        )),
    }
}

fn recovery_dispatch_response(
    response: MutationDispatchResponse,
) -> Result<RecoveryResponse, ManualSftpError> {
    match response {
        MutationDispatchResponse::Recovery(recovery) => Ok(recovery),
        _ => Err(invalid_response(
            "The mutation dispatcher returned the wrong response type.",
        )),
    }
}

fn upload_ready_dispatch_response(
    response: MutationDispatchResponse,
) -> Result<UploadReady, ManualSftpError> {
    match response {
        MutationDispatchResponse::UploadReady(ready) => Ok(ready),
        _ => Err(invalid_response(
            "The mutation dispatcher returned the wrong response type.",
        )),
    }
}

fn upload_chunk_dispatch_response(
    response: MutationDispatchResponse,
) -> Result<UploadChunkAck, ManualSftpError> {
    match response {
        MutationDispatchResponse::UploadChunk(chunk) => Ok(chunk),
        _ => Err(invalid_response(
            "The mutation dispatcher returned the wrong response type.",
        )),
    }
}

fn download_ready_dispatch_response(
    response: MutationDispatchResponse,
) -> Result<DownloadReady, ManualSftpError> {
    match response {
        MutationDispatchResponse::DownloadReady(ready) => Ok(ready),
        _ => Err(invalid_response(
            "The mutation dispatcher returned the wrong response type.",
        )),
    }
}

fn download_chunk_dispatch_response(
    response: MutationDispatchResponse,
) -> Result<DownloadChunk, ManualSftpError> {
    match response {
        MutationDispatchResponse::DownloadChunk(chunk) => Ok(chunk),
        _ => Err(invalid_response(
            "The mutation dispatcher returned the wrong response type.",
        )),
    }
}

fn local_error(error: super::local_files::LocalFileError) -> ManualSftpError {
    ManualSftpError::new(error.code(), "The local SFTP file operation failed.")
}

fn local_error_from_io(_: std::io::Error) -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_LOCAL_WRITE_FAILED",
        "The local download part could not be written.",
    )
}

fn local_handle_invalid() -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_LOCAL_HANDLE_INVALID",
        "The dedicated local file owner no longer has this handle.",
    )
}

fn local_worker_failed() -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_LOCAL_WORKER_FAILED",
        "The dedicated local file worker stopped unexpectedly.",
    )
}

fn mutation_dispatch_worker_failed() -> ManualSftpError {
    ManualSftpError::new(
        "SIDECAR_BROKER_CLOSED",
        "The dedicated mutation dispatch worker stopped unexpectedly.",
    )
}

fn journal_error(error: super::journal::JournalError) -> ManualSftpError {
    ManualSftpError::new(error.code(), "The local SFTP journal operation failed.")
}

fn invalid_response(message: &'static str) -> ManualSftpError {
    ManualSftpError::new("SIDECAR_RESPONSE_INVALID", message)
}

fn coordinator_closing() -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_COORDINATOR_CLOSING",
        "The manual SFTP coordinator is closing and cannot accept new work.",
    )
}

fn coordinator_closed() -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_COORDINATOR_CLOSED",
        "The manual SFTP coordinator is closed.",
    )
}

fn operation_cancelled() -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_OPERATION_CANCELLED",
        "The operation was cancelled before the next remote mutation.",
    )
}

fn mutation_outcome_unknown() -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_MUTATION_OUTCOME_UNKNOWN",
        "The remote mutation outcome could not be proven and will not be retried.",
    )
}

fn preparation_expiry_rfc3339() -> Result<String, ManualSftpError> {
    (OffsetDateTime::now_utc() + time::Duration::seconds(PREPARATION_TTL.as_secs() as i64))
        .format(&Rfc3339)
        .map_err(|_| {
            ManualSftpError::new(
                "SFTP_PREPARATION_TIME_INVALID",
                "The transfer preparation expiry could not be represented safely.",
            )
        })
}

fn state_from_terminal(state: OperationTerminalState) -> OperationState {
    match state {
        OperationTerminalState::Succeeded => OperationState::Succeeded,
        OperationTerminalState::Failed => OperationState::Failed,
        OperationTerminalState::Cancelled => OperationState::Cancelled,
        OperationTerminalState::CleanupRequired => OperationState::CleanupRequired,
        OperationTerminalState::OutcomeUnknown => OperationState::OutcomeUnknown,
    }
}

fn is_mutating_recovery_action(action: RecoveryAction) -> bool {
    matches!(
        action,
        RecoveryAction::DeleteTemp
            | RecoveryAction::ContinueDelete
            | RecoveryAction::RestoreTombstone
    )
}

fn recovery_from_record(
    record: LocalSftpOperationRecord,
) -> Result<RecoverySummary, ManualSftpError> {
    let kind = match record.kind {
        OperationKind::Upload => RecoveryKind::UploadTemp,
        OperationKind::Download => RecoveryKind::DownloadPart,
        OperationKind::RecursiveDelete => RecoveryKind::DeleteTombstone,
        OperationKind::Mkdir
        | OperationKind::Rename
        | OperationKind::Remove
        | OperationKind::Recovery => RecoveryKind::MutationUnknown,
    };
    let state = match record.state {
        OperationState::CleanupRequired => RecoveryState::CleanupRequired,
        OperationState::OutcomeUnknown => RecoveryState::OutcomeUnknown,
        _ => RecoveryState::RecoveryRequired,
    };
    let (display_name, available_actions) = if record.kind == OperationKind::Download {
        let display_name = record
            .local_path
            .as_deref()
            .and_then(std::path::Path::file_name)
            .map(|name| name.to_string_lossy().into_owned())
            .filter(|name| !name.is_empty())
            .ok_or_else(journal_invalid)?;
        (
            display_name,
            vec![
                RecoveryAction::Verify,
                RecoveryAction::OpenLocalFolder,
                RecoveryAction::Keep,
            ],
        )
    } else {
        (
            remote_display_name(&record.remote_path),
            vec![RecoveryAction::Verify, RecoveryAction::Keep],
        )
    };
    Ok(RecoverySummary {
        recovery_id: record.operation_id,
        operation_id: record.remote_operation_id.unwrap_or(record.operation_id),
        kind,
        host_label: record
            .host_label
            .unwrap_or_else(|| "Saved connection".to_owned()),
        remote_path: Some(record.remote_path.clone()),
        display_name,
        state,
        created_at: record.created_at.format(&Rfc3339).map_err(|_| {
            ManualSftpError::new(
                "SFTP_JOURNAL_INVALID",
                "The local recovery timestamp is invalid.",
            )
        })?,
        available_actions,
    })
}

fn remote_display_name(path: &str) -> String {
    path.rsplit('/')
        .find(|segment| !segment.is_empty())
        .unwrap_or("/")
        .to_owned()
}

fn journal_invalid() -> ManualSftpError {
    ManualSftpError::new(
        "SFTP_JOURNAL_INVALID",
        "The local SFTP recovery record is invalid.",
    )
}
