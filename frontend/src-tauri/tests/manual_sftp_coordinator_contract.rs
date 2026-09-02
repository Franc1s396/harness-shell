use std::{
    fs, fs::OpenOptions, os::windows::fs::OpenOptionsExt, path::PathBuf, pin::pin, sync::Arc,
    time::Duration,
};

use harness_shell_lib::sftp::{
    coordinator::{
        DeletePreflightInput, DownloadPreparationInput, LocalFileFinishFault,
        LocalFileFinishTestGate, LocalFileReplyTestGate, MkdirInput, MutatingDispatchTestGate,
        RemoveInput, RenameInput, SftpCoordinator, TransferProgressSink, TransferProgressSinkError,
        UploadPreparationInput,
    },
    journal::{
        JournalFaultTestGate, LocalSftpJournalActor, LocalSftpOperationJournal,
        LocalSftpOperationRecord, OperationKind, OperationState,
    },
    models::{
        ManualSftpError, OperationPhase, RecoveryAction, RecoveryKind, RecoveryState,
        TransferProgressProjection, TransferSnapshot, SFTP_CHUNK_BYTES,
    },
    protocol::ManualSftpRuntimeClient,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use time::OffsetDateTime;
use uuid::Uuid;
use windows_sys::Win32::Storage::FileSystem::{
    FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
};

#[path = "support/manual_sftp_http_harness.rs"]
mod manual_sftp_http_harness;
use manual_sftp_http_harness::{
    runtime_http_test_channel, test_bytes, HttpTestCommand, HttpTestResponse, HttpTestResponseKind,
};

#[derive(Default)]
struct RecordingTransferProgressSink {
    projections: std::sync::Mutex<Vec<TransferProgressProjection>>,
}

impl RecordingTransferProgressSink {
    fn projections(&self) -> Vec<TransferProgressProjection> {
        self.projections
            .lock()
            .expect("recording transfer-progress sink mutex poisoned")
            .clone()
    }
}

impl TransferProgressSink for RecordingTransferProgressSink {
    fn emit(
        &self,
        projection: TransferProgressProjection,
    ) -> Result<(), TransferProgressSinkError> {
        self.projections
            .lock()
            .expect("recording transfer-progress sink mutex poisoned")
            .push(projection);
        Ok(())
    }
}

#[derive(Default)]
struct FailingTransferProgressSink {
    attempts: std::sync::Mutex<usize>,
}

impl FailingTransferProgressSink {
    fn attempts(&self) -> usize {
        *self
            .attempts
            .lock()
            .expect("failing transfer-progress sink mutex poisoned")
    }
}

impl TransferProgressSink for FailingTransferProgressSink {
    fn emit(
        &self,
        _projection: TransferProgressProjection,
    ) -> Result<(), TransferProgressSinkError> {
        *self
            .attempts
            .lock()
            .expect("failing transfer-progress sink mutex poisoned") += 1;
        Err(TransferProgressSinkError::event_emit_failed())
    }
}

const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn object(value: Value) -> Map<String, Value> {
    value
        .as_object()
        .expect("fixture must be a JSON object")
        .clone()
}

fn response(request_id: Uuid, payload: Value) -> HttpTestResponse {
    HttpTestResponse {
        kind: HttpTestResponseKind::Response,
        request_id,
        payload: object(payload),
    }
}

fn error_response(request_id: Uuid, error_code: &str) -> HttpTestResponse {
    HttpTestResponse {
        kind: HttpTestResponseKind::Error,
        request_id,
        payload: object(json!({
            "error_code": error_code,
            "message": "The remote mutation was rejected."
        })),
    }
}

fn retained_state_error_response(
    request_id: Uuid,
    error_code: &str,
    operation_state: &str,
) -> HttpTestResponse {
    HttpTestResponse {
        kind: HttpTestResponseKind::Error,
        request_id,
        payload: object(json!({
            "error_code": error_code,
            "message": "Manual SFTP operation failed",
            "operation_state": operation_state
        })),
    }
}

fn upload_input(path: std::path::PathBuf) -> UploadPreparationInput {
    UploadPreparationInput {
        ssh_session_id: Uuid::new_v4(),
        connection_id: Uuid::new_v4(),
        local_path: path,
        remote_path: "/home/demo/payload.bin".to_owned(),
        host_label: "demo-host".to_owned(),
    }
}

fn mkdir_input() -> MkdirInput {
    MkdirInput {
        ssh_session_id: Uuid::new_v4(),
        connection_id: Uuid::new_v4(),
        parent_path: "/home/demo".to_owned(),
        name: "new-directory".to_owned(),
        host_label: "demo-host".to_owned(),
    }
}

fn rename_input() -> RenameInput {
    RenameInput {
        ssh_session_id: Uuid::new_v4(),
        connection_id: Uuid::new_v4(),
        source_path: "/home/demo/source.txt".to_owned(),
        target_path: "/home/demo/target.txt".to_owned(),
        overwrite: false,
        host_label: "demo-host".to_owned(),
    }
}

fn overwrite_rename_input() -> RenameInput {
    let mut input = rename_input();
    input.overwrite = true;
    input
}

fn remove_input() -> RemoveInput {
    RemoveInput {
        ssh_session_id: Uuid::new_v4(),
        connection_id: Uuid::new_v4(),
        path: "/home/demo/source.txt".to_owned(),
        host_label: "demo-host".to_owned(),
    }
}

fn delete_preflight_input() -> DeletePreflightInput {
    DeletePreflightInput {
        ssh_session_id: Uuid::new_v4(),
        connection_id: Uuid::new_v4(),
        path: "/home/demo/tree".to_owned(),
        host_label: "demo-host".to_owned(),
    }
}

fn download_input(path: std::path::PathBuf) -> DownloadPreparationInput {
    DownloadPreparationInput {
        ssh_session_id: Uuid::new_v4(),
        connection_id: Uuid::new_v4(),
        local_path: path,
        remote_path: "/home/demo/payload.bin".to_owned(),
        host_label: "demo-host".to_owned(),
    }
}

fn open_for_write(path: &std::path::Path) -> std::io::Result<fs::File> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .open(path)
}

fn coordinator_fixture() -> (
    SftpCoordinator,
    tokio::sync::mpsc::Receiver<HttpTestCommand>,
    tempfile::TempDir,
) {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, commands) = runtime_http_test_channel();
    (
        SftpCoordinator::new(ManualSftpRuntimeClient::new(broker), journal),
        commands,
        directory,
    )
}

fn coordinator_with_progress_fixture() -> (
    SftpCoordinator,
    tokio::sync::mpsc::Receiver<HttpTestCommand>,
    tempfile::TempDir,
    Arc<RecordingTransferProgressSink>,
) {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, commands) = runtime_http_test_channel();
    let progress = Arc::new(RecordingTransferProgressSink::default());
    (
        SftpCoordinator::new_with_progress_sink(
            ManualSftpRuntimeClient::new(broker),
            journal,
            progress.clone(),
        ),
        commands,
        directory,
        progress,
    )
}

async fn complete_recursive_delete_preflight(
    coordinator: &SftpCoordinator,
    commands: &mut tokio::sync::mpsc::Receiver<HttpTestCommand>,
) -> (Uuid, Uuid) {
    let delete_plan_id = Uuid::new_v4();
    let preflight = coordinator.preflight_delete(delete_preflight_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected delete preflight request");
        };
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "delete_plan": {
                        "delete_plan_id": delete_plan_id,
                        "operation_id": operation_id,
                        "root_path": "/home/demo/tree",
                        "root_snapshot": {
                            "path": "/home/demo/tree",
                            "exists": true,
                            "entry_type": "directory",
                            "size": null,
                            "mtime_ns": "1770000000000000000",
                            "sha256": null
                        },
                        "file_count": 0,
                        "directory_count": 1,
                        "symlink_count": 0,
                        "total_byte_count": 0,
                        "manifest_sha256": EMPTY_SHA256,
                        "complete": true
                    }
                }),
            )))
            .unwrap();
        operation_id
    };
    let (plan, operation_id) = tokio::join!(preflight, responder);
    assert_eq!(plan.unwrap().operation_id, operation_id);
    (delete_plan_id, operation_id)
}

async fn reply_to_upload_preflight(commands: &mut tokio::sync::mpsc::Receiver<HttpTestCommand>) {
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected runtime request");
    };
    assert_eq!(request.payload["operation"], "upload_preflight");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "snapshot": {
                    "path": "/home/demo/payload.bin",
                    "exists": false,
                    "entry_type": null,
                    "size": null,
                    "mtime_ns": null,
                    "sha256": null
                }
            }),
        )))
        .unwrap();
}

async fn reply_to_mkdir(commands: &mut tokio::sync::mpsc::Receiver<HttpTestCommand>) {
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected runtime request");
    };
    assert_eq!(request.payload["operation"], "mkdir");
    let operation_id = request.payload["params"]["operation_id"]
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .unwrap();
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "succeeded",
                    "error_code": null,
                    "message": "Created.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();
}

fn download_snapshot() -> Value {
    json!({
        "path": "/home/demo/payload.bin",
        "exists": true,
        "entry_type": "file",
        "size": 0,
        "mtime_ns": "1770000000000000000",
        "sha256": EMPTY_SHA256
    })
}

async fn reply_to_download_preflight(commands: &mut tokio::sync::mpsc::Receiver<HttpTestCommand>) {
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected remote hash request");
    };
    assert_eq!(request.payload["operation"], "sha256");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "hash": {
                    "path": "/home/demo/payload.bin",
                    "snapshot": download_snapshot(),
                    "sha256": EMPTY_SHA256,
                    "byte_count": 0
                }
            }),
        )))
        .unwrap();
}

async fn reply_to_download_preflight_with(
    commands: &mut tokio::sync::mpsc::Receiver<HttpTestCommand>,
    sha256: &str,
    byte_count: u64,
) {
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected remote hash request");
    };
    assert_eq!(request.payload["operation"], "sha256");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "hash": {
                    "path": "/home/demo/payload.bin",
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": byte_count,
                        "mtime_ns": "1770000000000000000",
                        "sha256": sha256
                    },
                    "sha256": sha256,
                    "byte_count": byte_count
                }
            }),
        )))
        .unwrap();
}

fn succeeded_terminal(operation_id: Uuid, sha256: &str, byte_count: u64) -> Value {
    json!({
        "terminal": {
            "operation_id": operation_id,
            "state": "succeeded",
            "error_code": null,
            "message": "Transfer completed.",
            "sha256": sha256,
            "byte_count": byte_count,
            "recovery_id": null
        }
    })
}

#[derive(Clone, Copy)]
enum UploadResponseFault {
    ChunkSequence,
    ChunkOffset,
    TerminalHash,
    TerminalCount,
}

async fn run_single_chunk_upload_fault(
    fault: UploadResponseFault,
) -> (SftpCoordinator, tempfile::TempDir, Uuid, ManualSftpError) {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("upload-fault.bin");
    let payload = b"upload-fault";
    let expected_hash = sha256(payload);
    fs::write(&source, payload).unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_upload(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected upload begin request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "upload": {
                        "operation_id": operation_id,
                        "temp_path": "/home/demo/.payload.bin.part",
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected upload chunk request");
        };
        assert_eq!(request.payload["operation"], "upload_chunk");
        let next_sequence = if matches!(fault, UploadResponseFault::ChunkSequence) {
            2
        } else {
            1
        };
        let next_offset = if matches!(fault, UploadResponseFault::ChunkOffset) {
            payload.len() as u64 + 1
        } else {
            payload.len() as u64
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "next_sequence": next_sequence,
                        "next_offset": next_offset
                    }
                }),
            )))
            .unwrap();
        if matches!(
            fault,
            UploadResponseFault::TerminalHash | UploadResponseFault::TerminalCount
        ) {
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected upload finish request");
            };
            assert_eq!(request.payload["operation"], "upload_finish");
            let terminal_hash = if matches!(fault, UploadResponseFault::TerminalHash) {
                EMPTY_SHA256
            } else {
                &expected_hash
            };
            let terminal_count = if matches!(fault, UploadResponseFault::TerminalCount) {
                payload.len() as u64 + 1
            } else {
                payload.len() as u64
            };
            reply
                .send(Ok(response(
                    request_id,
                    succeeded_terminal(operation_id, terminal_hash, terminal_count),
                )))
                .unwrap();
        }
    };
    let (result, ()) = tokio::join!(execute, responder);
    assert!(commands.try_recv().is_err(), "a failed upload was retried");
    (coordinator, directory, operation_id, result.unwrap_err())
}

#[derive(Clone, Copy)]
enum DownloadResponseFault {
    ChunkSequence,
    ChunkOffset,
    TerminalHash,
    TerminalCount,
}

async fn run_single_chunk_download_fault(
    fault: DownloadResponseFault,
) -> (
    SftpCoordinator,
    tempfile::TempDir,
    PathBuf,
    Uuid,
    ManualSftpError,
) {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let target = directory.path().join("download-fault.bin");
    let payload = b"download-fault";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_download(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download begin request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "download": {
                        "operation_id": operation_id,
                        "path": "/home/demo/payload.bin",
                        "snapshot": {
                            "path": "/home/demo/payload.bin",
                            "exists": true,
                            "entry_type": "file",
                            "size": payload.len(),
                            "mtime_ns": "1770000000000000000",
                            "sha256": expected_hash
                        },
                        "sha256": expected_hash,
                        "byte_count": payload.len(),
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download chunk request");
        };
        assert_eq!(request.payload["operation"], "download_chunk");
        let sequence = if matches!(fault, DownloadResponseFault::ChunkSequence) {
            1
        } else {
            0
        };
        let next_offset = if matches!(fault, DownloadResponseFault::ChunkOffset) {
            payload.len() as u64 + 1
        } else {
            payload.len() as u64
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "sequence": sequence,
                        "offset": 0,
                        "chunk_bytes": payload,
                        "next_offset": next_offset,
                        "eof": true
                    }
                }),
            )))
            .unwrap();
        if matches!(
            fault,
            DownloadResponseFault::TerminalHash | DownloadResponseFault::TerminalCount
        ) {
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected download finish request");
            };
            assert_eq!(request.payload["operation"], "download_finish");
            let terminal_hash = if matches!(fault, DownloadResponseFault::TerminalHash) {
                EMPTY_SHA256
            } else {
                &expected_hash
            };
            let terminal_count = if matches!(fault, DownloadResponseFault::TerminalCount) {
                payload.len() as u64 + 1
            } else {
                payload.len() as u64
            };
            reply
                .send(Ok(response(
                    request_id,
                    succeeded_terminal(operation_id, terminal_hash, terminal_count),
                )))
                .unwrap();
        }
    };
    let (result, ()) = tokio::join!(execute, responder);
    assert!(
        commands.try_recv().is_err(),
        "a failed download was retried"
    );
    (
        coordinator,
        directory,
        target,
        operation_id,
        result.unwrap_err(),
    )
}

async fn run_download_local_finish_fault(
    fault: LocalFileFinishFault,
) -> (
    SftpCoordinator,
    tempfile::TempDir,
    PathBuf,
    Uuid,
    ManualSftpError,
) {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let progress = Arc::new(RecordingTransferProgressSink::default());
    let finish_gate = LocalFileFinishTestGate::default();
    finish_gate.fail_next(fault);
    let coordinator = SftpCoordinator::new_with_progress_and_local_finish_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        progress,
        finish_gate,
    );
    let target = directory.path().join("local-finish-fault.bin");
    let payload = b"verified-download";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_download(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download begin request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "download": {
                        "operation_id": operation_id,
                        "path": "/home/demo/payload.bin",
                        "snapshot": {
                            "path": "/home/demo/payload.bin",
                            "exists": true,
                            "entry_type": "file",
                            "size": payload.len(),
                            "mtime_ns": "1770000000000000000",
                            "sha256": expected_hash
                        },
                        "sha256": expected_hash,
                        "byte_count": payload.len(),
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download chunk request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "sequence": 0,
                        "offset": 0,
                        "chunk_bytes": payload,
                        "next_offset": payload.len(),
                        "eof": true
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download finish request");
        };
        assert_eq!(request.payload["operation"], "download_finish");
        reply
            .send(Ok(response(
                request_id,
                succeeded_terminal(operation_id, &expected_hash, payload.len() as u64),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(execute, responder);
    assert!(
        commands.try_recv().is_err(),
        "a local finish fault triggered a retry"
    );
    (
        coordinator,
        directory,
        target,
        operation_id,
        result.unwrap_err(),
    )
}

fn recovery_record(operation_id: Uuid) -> LocalSftpOperationRecord {
    LocalSftpOperationRecord {
        operation_id,
        remote_operation_id: None,
        kind: OperationKind::Upload,
        state: OperationState::CleanupRequired,
        connection_id: Uuid::new_v4(),
        host_label: Some("demo-host".to_owned()),
        local_path: Some(PathBuf::from(r"C:\recovery\payload.bin")),
        remote_path: "/home/demo/payload.bin".to_owned(),
        expected_sha256: Some(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
        ),
        target_snapshot: Some(TransferSnapshot {
            path: "/home/demo/payload.bin".to_owned(),
            exists: false,
            entry_type: None,
            size: None,
            mtime_ns: None,
            sha256: None,
        }),
        created_at: OffsetDateTime::now_utc(),
    }
}

fn recovery_summary(operation_id: Uuid) -> Value {
    json!({
        "recovery": {
            "recovery_id": operation_id,
            "operation_id": operation_id,
            "kind": "upload_temp",
            "host_label": "demo-host",
            "remote_path": "/home/demo/payload.bin",
            "display_name": "payload.bin",
            "state": "cleanup_required",
            "created_at": "2026-08-29T00:00:00Z",
            "available_actions": ["delete_temp", "keep"]
        }
    })
}

fn recovery_coordinator_fixture() -> (
    SftpCoordinator,
    tokio::sync::mpsc::Receiver<HttpTestCommand>,
    tempfile::TempDir,
    Uuid,
) {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let old_operation_id = Uuid::new_v4();
    journal.put(&recovery_record(old_operation_id)).unwrap();
    let (broker, commands) = runtime_http_test_channel();
    (
        SftpCoordinator::new(ManualSftpRuntimeClient::new(broker), journal),
        commands,
        directory,
        old_operation_id,
    )
}

#[tokio::test]
async fn upload_preparation_emits_only_frozen_safe_progress() {
    let (coordinator, mut commands, directory, progress) = coordinator_with_progress_fixture();
    let source = directory.path().join("source.bin");
    fs::write(&source, b"progress-contract").unwrap();

    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let projections = progress.projections();

    assert_eq!(projections.len(), 1);
    let projection = &projections[0];
    assert_eq!(projection.operation_id, prepared.operation_id);
    assert_eq!(projection.phase, OperationPhase::Preparing);
    assert_eq!(projection.bytes_completed, 0);
    assert_eq!(projection.bytes_total, 17);
    assert!(projection.cancellable);
    let value = serde_json::to_value(projection).unwrap();
    assert_eq!(value.as_object().unwrap().len(), 9);
    for field in [
        "operation_id",
        "direction",
        "phase",
        "display_name",
        "remote_path",
        "host_label",
        "bytes_completed",
        "bytes_total",
        "cancellable",
    ] {
        assert!(value.get(field).is_some(), "missing safe field {field}");
    }
    assert!(!value
        .to_string()
        .contains(directory.path().to_str().unwrap()));
}

#[tokio::test]
async fn upload_preparation_summary_has_the_exact_safe_frozen_contract() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("upload-summary.bin");
    let payload = b"upload-summary";
    fs::write(&source, payload).unwrap();
    let expected_hash = sha256(payload);
    let prepare = coordinator.prepare_upload(upload_input(source));
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected upload preflight request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": 9,
                        "mtime_ns": "1770000000000000000",
                        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    }
                }),
            )))
            .unwrap();
    };
    let (summary, ()) = tokio::join!(prepare, responder);
    let summary = summary.unwrap();
    let value = serde_json::to_value(&summary).unwrap();

    assert_eq!(value.as_object().unwrap().len(), 11);
    assert_eq!(summary.source_sha256, expected_hash);
    assert_eq!(summary.source_byte_count, payload.len() as u64);
    assert!(summary.overwrite_required);
    assert_eq!(summary.target_snapshot.path, "/home/demo/payload.bin");
    OffsetDateTime::parse(
        &summary.expires_at,
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    assert!(!value
        .to_string()
        .contains(directory.path().to_str().unwrap()));

    let error = coordinator
        .execute_upload(summary.preparation_id, false)
        .await
        .unwrap_err();
    assert_eq!(error.code(), "SFTP_CONFIRMATION_REQUIRED");
    assert!(commands.try_recv().is_err());
}

#[tokio::test]
async fn download_preparation_summary_has_the_exact_safe_frozen_contract() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let target = directory.path().join("download-summary.bin");
    fs::write(&target, b"existing-local-target").unwrap();
    let (summary, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target)),
        reply_to_download_preflight(&mut commands)
    );
    let summary = summary.unwrap();
    let value = serde_json::to_value(&summary).unwrap();

    assert_eq!(value.as_object().unwrap().len(), 11);
    assert_eq!(summary.source_sha256, EMPTY_SHA256);
    assert_eq!(summary.source_byte_count, 0);
    assert!(summary.overwrite_required);
    assert!(summary.target_snapshot.exists);
    assert_eq!(summary.target_snapshot.path, "download-summary.bin");
    assert!(!summary.target_snapshot.path.contains(':'));
    OffsetDateTime::parse(
        &summary.expires_at,
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    assert!(!value
        .to_string()
        .contains(directory.path().to_str().unwrap()));
}

#[tokio::test]
async fn progress_emission_failure_is_diagnostic_and_does_not_orphan_remote_transfer() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let progress = Arc::new(FailingTransferProgressSink::default());
    let coordinator = SftpCoordinator::new_with_progress_sink(
        ManualSftpRuntimeClient::new(broker),
        journal,
        progress.clone(),
    );
    let source = directory.path().join("empty.bin");
    fs::write(&source, []).unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_upload(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected upload begin request");
        };
        assert_eq!(request.payload["operation"], "upload_begin");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "upload": {
                        "operation_id": operation_id,
                        "temp_path": "/home/demo/.payload.bin.part",
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected upload finish request");
        };
        assert_eq!(request.payload["operation"], "upload_finish");
        reply
            .send(Ok(response(
                request_id,
                succeeded_terminal(operation_id, EMPTY_SHA256, 0),
            )))
            .unwrap();
    };
    let (terminal, ()) = tokio::join!(execute, responder);

    assert_eq!(terminal.unwrap().operation_id, operation_id);
    assert_eq!(progress.attempts(), 3);
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
    assert!(coordinator.gates_are_free());
}

#[tokio::test]
async fn successful_multichunk_upload_verifies_every_frame_and_emits_coherent_progress() {
    let (coordinator, mut commands, directory, progress) = coordinator_with_progress_fixture();
    let source = directory.path().join("upload.bin");
    let payload = (0..SFTP_CHUNK_BYTES + 17)
        .map(|index| (index % 251) as u8)
        .collect::<Vec<_>>();
    let expected_hash = sha256(&payload);
    fs::write(&source, &payload).unwrap();

    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_upload(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected upload begin request");
        };
        assert_eq!(request.payload["operation"], "upload_begin");
        assert_eq!(
            request.payload["params"]["operation_id"],
            operation_id.to_string()
        );
        assert_eq!(
            request.payload["params"]["source_byte_count"],
            payload.len() as u64
        );
        assert_eq!(request.payload["params"]["source_sha256"], expected_hash);
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "upload": {
                        "operation_id": operation_id,
                        "temp_path": "/home/demo/.payload.bin.part",
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();

        let mut offset = 0_usize;
        for sequence in 0..2_u32 {
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected upload chunk request");
            };
            assert_eq!(request.payload["operation"], "upload_chunk");
            assert_eq!(request.payload["params"]["sequence"], sequence);
            assert_eq!(request.payload["params"]["offset"], offset as u64);
            let actual = test_bytes(&request.payload["params"]["chunk_bytes"]);
            let end = (offset + SFTP_CHUNK_BYTES).min(payload.len());
            assert_eq!(actual, payload[offset..end]);
            offset = end;
            reply
                .send(Ok(response(
                    request_id,
                    json!({
                        "chunk": {
                            "operation_id": operation_id,
                            "next_sequence": sequence + 1,
                            "next_offset": offset
                        }
                    }),
                )))
                .unwrap();
        }

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected upload finish request");
        };
        assert_eq!(request.payload["operation"], "upload_finish");
        reply
            .send(Ok(response(
                request_id,
                succeeded_terminal(operation_id, &expected_hash, payload.len() as u64),
            )))
            .unwrap();
    };
    let (terminal, ()) = tokio::join!(execute, responder);
    let terminal = terminal.unwrap();

    assert_eq!(terminal.operation_id, operation_id);
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
    assert!(coordinator.gates_are_free());
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    let projections = progress.projections();
    assert_eq!(
        projections
            .iter()
            .map(|projection| (
                projection.phase,
                projection.bytes_completed,
                projection.cancellable
            ))
            .collect::<Vec<_>>(),
        vec![
            (OperationPhase::Preparing, 0, true),
            (OperationPhase::Transferring, 0, true),
            (OperationPhase::Transferring, SFTP_CHUNK_BYTES as u64, true),
            (OperationPhase::Transferring, payload.len() as u64, true),
            (OperationPhase::Committing, payload.len() as u64, false),
        ]
    );
    assert!(projections
        .iter()
        .all(|projection| projection.bytes_total == payload.len() as u64));
    let reopened =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    assert!(reopened.get(operation_id).unwrap().is_none());
}

#[tokio::test]
async fn successful_multichunk_download_commits_locally_and_emits_coherent_progress() {
    let (coordinator, mut commands, directory, progress) = coordinator_with_progress_fixture();
    let target = directory.path().join("download.bin");
    let payload = (0..SFTP_CHUNK_BYTES + 17)
        .map(|index| (index % 239) as u8)
        .collect::<Vec<_>>();
    let expected_hash = sha256(&payload);
    let input = download_input(target.clone());
    let ssh_session_id = input.ssh_session_id;

    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(input),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_download(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download begin request");
        };
        assert_eq!(request.payload["operation"], "download_begin");
        let active = coordinator
            .active_transfer_for_session(ssh_session_id)
            .expect("the active transfer must be projected for its SSH session");
        assert_eq!(active.operation_id, operation_id);
        assert_eq!(active.phase, OperationPhase::Transferring);
        assert!(active.cancellable);
        assert!(coordinator
            .active_transfer_for_session(Uuid::new_v4())
            .is_none());
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "download": {
                        "operation_id": operation_id,
                        "path": "/home/demo/payload.bin",
                        "snapshot": {
                            "path": "/home/demo/payload.bin",
                            "exists": true,
                            "entry_type": "file",
                            "size": payload.len(),
                            "mtime_ns": "1770000000000000000",
                            "sha256": expected_hash
                        },
                        "sha256": expected_hash,
                        "byte_count": payload.len(),
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();

        let mut offset = 0_usize;
        for sequence in 0..2_u32 {
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected download chunk request");
            };
            assert_eq!(request.payload["operation"], "download_chunk");
            assert_eq!(request.payload["params"]["sequence"], sequence);
            assert_eq!(request.payload["params"]["offset"], offset as u64);
            let end = (offset + SFTP_CHUNK_BYTES).min(payload.len());
            reply
                .send(Ok(response(
                    request_id,
                    json!({
                        "chunk": {
                            "operation_id": operation_id,
                            "sequence": sequence,
                            "offset": offset,
                            "chunk_bytes": &payload[offset..end],
                            "next_offset": end,
                            "eof": end == payload.len()
                        }
                    }),
                )))
                .unwrap();
            offset = end;
        }

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download finish request");
        };
        assert_eq!(request.payload["operation"], "download_finish");
        reply
            .send(Ok(response(
                request_id,
                succeeded_terminal(operation_id, &expected_hash, payload.len() as u64),
            )))
            .unwrap();
    };
    let (terminal, ()) = tokio::join!(execute, responder);
    terminal.unwrap();

    assert_eq!(fs::read(&target).unwrap(), payload);
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
    assert!(coordinator.gates_are_free());
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    let projections = progress.projections();
    assert_eq!(
        projections
            .iter()
            .map(|projection| (
                projection.phase,
                projection.bytes_completed,
                projection.cancellable
            ))
            .collect::<Vec<_>>(),
        vec![
            (OperationPhase::Preparing, 0, true),
            (OperationPhase::Transferring, 0, true),
            (OperationPhase::Transferring, SFTP_CHUNK_BYTES as u64, true),
            (OperationPhase::Transferring, payload.len() as u64, true),
            (OperationPhase::Verifying, payload.len() as u64, true),
            (OperationPhase::Committing, payload.len() as u64, false),
        ]
    );
    assert!(projections
        .iter()
        .all(|projection| projection.bytes_total == payload.len() as u64));
    let reopened =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    assert!(reopened.get(operation_id).unwrap().is_none());
}

#[tokio::test]
async fn zero_byte_download_skips_chunks_and_commits_after_finish() {
    let (coordinator, mut commands, directory, progress) = coordinator_with_progress_fixture();
    let target = directory.path().join("zero-byte.bin");
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_download(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download begin request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "download": {
                        "operation_id": operation_id,
                        "path": "/home/demo/payload.bin",
                        "snapshot": download_snapshot(),
                        "sha256": EMPTY_SHA256,
                        "byte_count": 0,
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download finish request");
        };
        assert_eq!(request.payload["operation"], "download_finish");
        reply
            .send(Ok(response(
                request_id,
                succeeded_terminal(operation_id, EMPTY_SHA256, 0),
            )))
            .unwrap();
    };
    let (terminal, ()) = tokio::join!(execute, responder);

    terminal.unwrap();
    assert_eq!(fs::read(target).unwrap(), b"");
    assert_eq!(
        progress
            .projections()
            .iter()
            .map(|projection| projection.phase)
            .collect::<Vec<_>>(),
        vec![
            OperationPhase::Preparing,
            OperationPhase::Transferring,
            OperationPhase::Verifying,
            OperationPhase::Committing,
        ]
    );
    assert!(commands.try_recv().is_err());
}

#[tokio::test]
async fn upload_rejects_chunk_and_terminal_mismatches_without_retry() {
    for fault in [
        UploadResponseFault::ChunkSequence,
        UploadResponseFault::ChunkOffset,
        UploadResponseFault::TerminalHash,
        UploadResponseFault::TerminalCount,
    ] {
        let (coordinator, _directory, operation_id, error) =
            run_single_chunk_upload_fault(fault).await;
        assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
        let recoveries = coordinator.list_recoveries().await.unwrap();
        assert_eq!(recoveries.len(), 1);
        assert_eq!(recoveries[0].operation_id, operation_id);
        assert_eq!(recoveries[0].host_label, "demo-host");
        assert_eq!(recoveries[0].display_name, "payload.bin");
        assert_eq!(recoveries[0].state, RecoveryState::OutcomeUnknown);
        assert!(coordinator.gates_are_free());
        assert_eq!(
            coordinator.local_file_owner_count_for_test().await.unwrap(),
            0
        );
    }
}

#[tokio::test]
async fn download_rejects_chunk_and_terminal_mismatches_without_local_commit_or_retry() {
    for fault in [
        DownloadResponseFault::ChunkSequence,
        DownloadResponseFault::ChunkOffset,
        DownloadResponseFault::TerminalHash,
        DownloadResponseFault::TerminalCount,
    ] {
        let (coordinator, directory, target, operation_id, error) =
            run_single_chunk_download_fault(fault).await;
        assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
        assert!(!target.exists());
        let part = directory
            .path()
            .join(format!(".harness-shell-download-{operation_id}.part"));
        assert!(part.exists());
        let recoveries = coordinator.list_recoveries().await.unwrap();
        assert_eq!(recoveries.len(), 1);
        assert_eq!(recoveries[0].operation_id, operation_id);
        assert_eq!(recoveries[0].state, RecoveryState::OutcomeUnknown);
        assert!(coordinator.gates_are_free());
        assert_eq!(
            coordinator.local_file_owner_count_for_test().await.unwrap(),
            0
        );
    }
}

#[tokio::test]
async fn download_rechecks_local_target_immediately_before_atomic_commit() {
    let (coordinator, mut commands, directory, progress) = coordinator_with_progress_fixture();
    let target = directory.path().join("changed-target.bin");
    let payload = b"trusted-remote-content";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_download(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download begin request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "download": {
                        "operation_id": operation_id,
                        "path": "/home/demo/payload.bin",
                        "snapshot": {
                            "path": "/home/demo/payload.bin",
                            "exists": true,
                            "entry_type": "file",
                            "size": payload.len(),
                            "mtime_ns": "1770000000000000000",
                            "sha256": expected_hash
                        },
                        "sha256": expected_hash,
                        "byte_count": payload.len(),
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download chunk request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "sequence": 0,
                        "offset": 0,
                        "chunk_bytes": payload,
                        "next_offset": payload.len(),
                        "eof": true
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download finish request");
        };
        assert_eq!(request.payload["operation"], "download_finish");
        fs::write(&target, b"external-writer-won").unwrap();
        reply
            .send(Ok(response(
                request_id,
                succeeded_terminal(operation_id, &expected_hash, payload.len() as u64),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(execute, responder);
    let error = result.unwrap_err();

    assert_eq!(error.code(), "SFTP_TARGET_CHANGED");
    assert_eq!(fs::read(&target).unwrap(), b"external-writer-won");
    assert!(
        commands.try_recv().is_err(),
        "target change triggered a retry"
    );
    let recoveries = coordinator.list_recoveries().await.unwrap();
    assert_eq!(recoveries.len(), 1);
    assert_eq!(recoveries[0].operation_id, operation_id);
    assert_eq!(recoveries[0].state, RecoveryState::CleanupRequired);
    assert!(directory
        .path()
        .join(format!(".harness-shell-download-{operation_id}.part"))
        .exists());
    assert_eq!(
        progress.projections().last().unwrap().phase,
        OperationPhase::Committing
    );
    assert!(!progress.projections().last().unwrap().cancellable);
    assert!(coordinator.gates_are_free());
}

#[tokio::test]
async fn download_local_finish_faults_are_durable_and_never_retried() {
    for (fault, expected_code) in [
        (LocalFileFinishFault::Sync, "SFTP_LOCAL_SYNC_FAILED"),
        (
            LocalFileFinishFault::AtomicCommit,
            "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
        ),
    ] {
        let (coordinator, directory, target, operation_id, error) =
            run_download_local_finish_fault(fault).await;
        assert_eq!(error.code(), expected_code);
        assert!(!target.exists());
        assert!(directory
            .path()
            .join(format!(".harness-shell-download-{operation_id}.part"))
            .exists());
        let recoveries = coordinator.list_recoveries().await.unwrap();
        assert_eq!(recoveries.len(), 1);
        assert_eq!(recoveries[0].operation_id, operation_id);
        assert_eq!(recoveries[0].state, RecoveryState::CleanupRequired);
        assert!(coordinator.gates_are_free());
        assert_eq!(
            coordinator.local_file_owner_count_for_test().await.unwrap(),
            0
        );
    }
}

#[tokio::test]
async fn cancellation_after_download_finish_prevents_the_local_commit() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let target = directory.path().join("cancel-before-commit.bin");
    let payload = b"cancel-before-commit";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute = coordinator.execute_download(prepared.preparation_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download begin request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "download": {
                        "operation_id": operation_id,
                        "path": "/home/demo/payload.bin",
                        "snapshot": {
                            "path": "/home/demo/payload.bin",
                            "exists": true,
                            "entry_type": "file",
                            "size": payload.len(),
                            "mtime_ns": "1770000000000000000",
                            "sha256": expected_hash
                        },
                        "sha256": expected_hash,
                        "byte_count": payload.len(),
                        "next_sequence": 0,
                        "next_offset": 0
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download chunk request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "sequence": 0,
                        "offset": 0,
                        "chunk_bytes": payload,
                        "next_offset": payload.len(),
                        "eof": true
                    }
                }),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected download finish request");
        };
        assert_eq!(request.payload["operation"], "download_finish");
        coordinator.cancel(operation_id).unwrap();
        reply
            .send(Ok(response(
                request_id,
                succeeded_terminal(operation_id, &expected_hash, payload.len() as u64),
            )))
            .unwrap();
    };
    let (terminal, ()) = tokio::join!(execute, responder);
    let terminal = terminal.unwrap();

    assert_eq!(
        terminal.state,
        harness_shell_lib::sftp::models::OperationTerminalState::Cancelled
    );
    assert!(!target.exists());
    assert!(!directory
        .path()
        .join(format!(".harness-shell-download-{operation_id}.part"))
        .exists());
    assert!(commands.try_recv().is_err());
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
    assert!(coordinator.gates_are_free());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cancellation_is_too_late_once_local_commit_has_started() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("committing.bin");
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let progress = Arc::new(RecordingTransferProgressSink::default());
    let finish_gate = LocalFileFinishTestGate::default();
    finish_gate.block_next_finish();
    let coordinator = Arc::new(
        SftpCoordinator::new_with_progress_and_local_finish_test_gate(
            ManualSftpRuntimeClient::new(broker),
            journal,
            progress.clone(),
            finish_gate.clone(),
        ),
    );
    let payload = b"committing-is-final";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let execute_coordinator = coordinator.clone();
    let execute = tokio::spawn(async move {
        execute_coordinator
            .execute_download(prepared.preparation_id, true)
            .await
    });

    let HttpTestCommand::Request {
        request_id, reply, ..
    } = commands.recv().await.unwrap()
    else {
        panic!("expected download begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "download": {
                    "operation_id": operation_id,
                    "path": "/home/demo/payload.bin",
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": payload.len(),
                        "mtime_ns": "1770000000000000000",
                        "sha256": expected_hash
                    },
                    "sha256": expected_hash,
                    "byte_count": payload.len(),
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = commands.recv().await.unwrap()
    else {
        panic!("expected download chunk request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "chunk": {
                    "operation_id": operation_id,
                    "sequence": 0,
                    "offset": 0,
                    "chunk_bytes": payload,
                    "next_offset": payload.len(),
                    "eof": true
                }
            }),
        )))
        .unwrap();
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected download finish request");
    };
    assert_eq!(request.payload["operation"], "download_finish");
    reply
        .send(Ok(response(
            request_id,
            succeeded_terminal(operation_id, &expected_hash, payload.len() as u64),
        )))
        .unwrap();

    finish_gate.wait_until_blocked().await;
    let error = coordinator.cancel(operation_id).unwrap_err();
    assert_eq!(error.code(), "SFTP_CANCEL_TOO_LATE");
    assert_eq!(
        progress.projections().last().unwrap().phase,
        OperationPhase::Committing
    );
    assert!(!progress.projections().last().unwrap().cancellable);
    finish_gate.release();
    execute.await.unwrap().unwrap();

    assert_eq!(fs::read(target).unwrap(), payload);
    assert!(commands.try_recv().is_err());
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
    assert!(coordinator.gates_are_free());
}

#[tokio::test]
async fn dropping_upload_after_remote_begin_dispatches_abort_and_deletes_the_journal() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("drop-upload.bin");
    fs::write(&source, b"drop-after-begin").unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let mut execute = Box::pin(coordinator.execute_upload(prepared.preparation_id, true));

    let command = tokio::select! {
        result = &mut execute => panic!("upload completed before begin: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = command
    else {
        panic!("expected upload begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "upload": {
                    "operation_id": operation_id,
                    "temp_path": "/home/demo/.payload.bin.part",
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let command = tokio::select! {
        result = &mut execute => panic!("upload completed before chunk dispatch: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = command
    else {
        panic!("expected upload chunk request");
    };
    drop(execute);
    assert!(
        commands.try_recv().is_err(),
        "abort must wait until the in-flight chunk request settles"
    );
    reply
        .send(Ok(error_response(
            request_id,
            "SFTP_PROTOCOL_SEQUENCE_INVALID",
        )))
        .unwrap();

    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = tokio::time::timeout(Duration::from_secs(2), commands.recv())
        .await
        .expect("dropped upload did not dispatch abort")
        .unwrap()
    else {
        panic!("expected upload abort request");
    };
    assert_eq!(request.payload["operation"], "upload_abort");
    let mut shutdown = Box::pin(coordinator.shutdown());
    tokio::select! {
        result = &mut shutdown => {
            panic!("shutdown returned before the detached upload abort settled: {result:?}")
        }
        _ = tokio::task::yield_now() => {}
    }
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Upload aborted.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let reopened =
                LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3"))
                    .unwrap();
            if reopened.get(operation_id).unwrap().is_none() {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("confirmed detached upload abort did not delete the journal");
    assert!(shutdown.await.drained());
}

#[tokio::test]
async fn dropping_download_after_remote_begin_aborts_remote_and_local_part() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let target = directory.path().join("drop-download.bin");
    let payload = b"drop-download";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let part = directory
        .path()
        .join(format!(".harness-shell-download-{operation_id}.part"));
    let mut execute = Box::pin(coordinator.execute_download(prepared.preparation_id, true));

    let command = tokio::select! {
        result = &mut execute => panic!("download completed before begin: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = command
    else {
        panic!("expected download begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "download": {
                    "operation_id": operation_id,
                    "path": "/home/demo/payload.bin",
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": payload.len(),
                        "mtime_ns": "1770000000000000000",
                        "sha256": expected_hash
                    },
                    "sha256": expected_hash,
                    "byte_count": payload.len(),
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let command = tokio::select! {
        result = &mut execute => panic!("download completed before chunk dispatch: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = command
    else {
        panic!("expected download chunk request");
    };
    assert!(part.exists());
    drop(execute);
    assert!(
        commands.try_recv().is_err(),
        "abort must wait until the in-flight chunk request settles"
    );
    reply
        .send(Ok(error_response(
            request_id,
            "SFTP_PROTOCOL_SEQUENCE_INVALID",
        )))
        .unwrap();

    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = tokio::time::timeout(Duration::from_secs(2), commands.recv())
        .await
        .expect("dropped download did not dispatch abort")
        .unwrap()
    else {
        panic!("expected download abort request");
    };
    assert_eq!(request.payload["operation"], "download_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Download aborted.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let reopened =
                LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3"))
                    .unwrap();
            if reopened.get(operation_id).unwrap().is_none() && !part.exists() {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("confirmed detached download abort did not clean local and durable state");
    assert!(!target.exists());
}

#[tokio::test]
async fn upload_local_read_failure_aborts_remote_and_returns_the_safe_original_error() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let fault = LocalFileFinishTestGate::default();
    fault.fail_next(LocalFileFinishFault::UploadRead);
    let coordinator = SftpCoordinator::new_with_progress_and_local_finish_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        Arc::new(RecordingTransferProgressSink::default()),
        fault,
    );
    let source = directory.path().join("read-failure.bin");
    fs::write(&source, b"read-failure").unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let mut execute = Box::pin(coordinator.execute_upload(prepared.preparation_id, true));

    let HttpTestCommand::Request {
        request_id, reply, ..
    } = tokio::select! (
        result = &mut execute => panic!("upload completed before begin: {result:?}"),
        command = commands.recv() => command.unwrap(),
    )
    else {
        panic!("expected upload begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "upload": {
                    "operation_id": operation_id,
                    "temp_path": "/home/demo/.payload.bin.part",
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = tokio::select! (
        result = &mut execute => panic!("local read failure returned before remote abort: {result:?}"),
        command = commands.recv() => command.unwrap(),
    )
    else {
        panic!("expected upload abort request");
    };
    assert_eq!(request.payload["operation"], "upload_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Upload aborted.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    let error = execute.await.unwrap_err();
    assert_eq!(error.code(), "SFTP_LOCAL_READ_FAILED");
    assert!(
        commands.try_recv().is_err(),
        "local read failure was retried"
    );
    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert!(reopened.get(operation_id).unwrap().is_none());
}

async fn run_download_local_io_failure(
    fault_kind: LocalFileFinishFault,
    abort_cleanup_failure: bool,
) {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let fault = LocalFileFinishTestGate::default();
    if abort_cleanup_failure {
        fault.fail_sequence(&[fault_kind, LocalFileFinishFault::PartAbort]);
    } else {
        fault.fail_next(fault_kind);
    }
    let coordinator = SftpCoordinator::new_with_progress_and_local_finish_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        Arc::new(RecordingTransferProgressSink::default()),
        fault,
    );
    let target = directory.path().join("local-io-failure.bin");
    let payload = b"local-io-failure";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let part = directory
        .path()
        .join(format!(".harness-shell-download-{operation_id}.part"));
    let mut execute = Box::pin(coordinator.execute_download(prepared.preparation_id, true));

    let begin = tokio::select! {
        result = &mut execute => panic!("download completed before begin: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = begin
    else {
        panic!("expected download begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "download": {
                    "operation_id": operation_id,
                    "path": "/home/demo/payload.bin",
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": payload.len(),
                        "mtime_ns": "1770000000000000000",
                        "sha256": expected_hash
                    },
                    "sha256": expected_hash,
                    "byte_count": payload.len(),
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();

    if fault_kind == LocalFileFinishFault::PartWrite {
        let chunk = tokio::select! {
            result = &mut execute => panic!("download completed before chunk: {result:?}"),
            command = commands.recv() => command.unwrap(),
        };
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = chunk
        else {
            panic!("expected download chunk request");
        };
        assert_eq!(request.payload["operation"], "download_chunk");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "sequence": 0,
                        "offset": 0,
                        "chunk_bytes": payload,
                        "next_offset": payload.len(),
                        "eof": true
                    }
                }),
            )))
            .unwrap();
    }

    let abort = tokio::select! {
        result = &mut execute => panic!("local I/O failure returned before remote abort: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = abort
    else {
        panic!("expected download abort request");
    };
    assert_eq!(request.payload["operation"], "download_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Download aborted.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    let error = execute.await.unwrap_err();
    let original_code = match fault_kind {
        LocalFileFinishFault::PartCreate => "SFTP_LOCAL_PART_CREATE_FAILED",
        LocalFileFinishFault::PartWrite => "SFTP_LOCAL_WRITE_FAILED",
        _ => unreachable!(),
    };
    let expected_code = if abort_cleanup_failure {
        "SFTP_LOCAL_CLEANUP_FAILED"
    } else {
        original_code
    };
    assert_eq!(error.code(), expected_code);
    assert_eq!(part.exists(), abort_cleanup_failure);
    assert!(!target.exists());
    assert!(
        commands.try_recv().is_err(),
        "local I/O failure was retried"
    );
    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    if abort_cleanup_failure {
        assert_eq!(
            reopened.get(operation_id).unwrap().unwrap().state,
            OperationState::CleanupRequired
        );
    } else {
        assert!(reopened.get(operation_id).unwrap().is_none());
    }
}

#[tokio::test]
async fn download_local_create_failure_aborts_remote_and_deletes_the_journal() {
    run_download_local_io_failure(LocalFileFinishFault::PartCreate, false).await;
}

#[tokio::test]
async fn download_local_write_failure_aborts_remote_and_removes_the_part() {
    run_download_local_io_failure(LocalFileFinishFault::PartWrite, false).await;
}

#[tokio::test]
async fn failed_local_abort_is_durable_cleanup_required() {
    run_download_local_io_failure(LocalFileFinishFault::PartWrite, true).await;
}

#[tokio::test]
async fn untrusted_abort_after_local_failure_is_durable_outcome_unknown() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let fault = LocalFileFinishTestGate::default();
    fault.fail_next(LocalFileFinishFault::UploadRead);
    let coordinator = SftpCoordinator::new_with_progress_and_local_finish_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        Arc::new(RecordingTransferProgressSink::default()),
        fault,
    );
    let source = directory.path().join("untrusted-abort.bin");
    fs::write(&source, b"untrusted-abort").unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let mut execute = Box::pin(coordinator.execute_upload(prepared.preparation_id, true));

    let begin = tokio::select! {
        result = &mut execute => panic!("upload completed before begin: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = begin
    else {
        panic!("expected upload begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "upload": {
                    "operation_id": operation_id,
                    "temp_path": "/home/demo/.payload.bin.part",
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let abort = tokio::select! {
        result = &mut execute => panic!("local read failure returned before remote abort: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = abort
    else {
        panic!("expected upload abort request");
    };
    assert_eq!(request.payload["operation"], "upload_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": Uuid::new_v4(),
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Untrusted abort receipt.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    let error = execute.await.unwrap_err();
    assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert!(commands.try_recv().is_err(), "untrusted abort was retried");
    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert_eq!(
        reopened.get(operation_id).unwrap().unwrap().state,
        OperationState::OutcomeUnknown
    );
}

#[tokio::test]
async fn upload_journal_transition_failure_after_begin_still_aborts_remote() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let journal_fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        journal_fault.clone(),
    );
    let source = directory.path().join("journal-transition.bin");
    let payload = b"journal-transition";
    fs::write(&source, payload).unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let mut execute = Box::pin(coordinator.execute_upload(prepared.preparation_id, true));

    let begin = tokio::select! {
        result = &mut execute => panic!("upload completed before begin: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = begin
    else {
        panic!("expected upload begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "upload": {
                    "operation_id": operation_id,
                    "temp_path": "/home/demo/.payload.bin.part",
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let chunk = tokio::select! {
        result = &mut execute => panic!("upload completed before chunk: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = chunk
    else {
        panic!("expected upload chunk request");
    };
    assert_eq!(request.payload["operation"], "upload_chunk");
    journal_fault.fail_next_put();
    reply
        .send(Ok(response(
            request_id,
            json!({
                "chunk": {
                    "operation_id": operation_id,
                    "next_sequence": 1,
                    "next_offset": payload.len()
                }
            }),
        )))
        .unwrap();

    let abort = tokio::select! {
        result = &mut execute => panic!("journal failure returned before remote abort: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = abort
    else {
        panic!("expected upload abort request");
    };
    assert_eq!(request.payload["operation"], "upload_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Upload aborted.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    let error = execute.await.unwrap_err();
    assert_eq!(error.code(), "SFTP_JOURNAL_UNAVAILABLE");
    assert!(commands.try_recv().is_err(), "journal failure was retried");
    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert!(reopened.get(operation_id).unwrap().is_none());
}

#[tokio::test]
async fn download_journal_transition_failure_after_begin_aborts_remote_and_local_part() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let journal_fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        journal_fault.clone(),
    );
    let target = directory.path().join("journal-transition-download.bin");
    let payload = b"journal-transition-download";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let part = directory
        .path()
        .join(format!(".harness-shell-download-{operation_id}.part"));
    let mut execute = Box::pin(coordinator.execute_download(prepared.preparation_id, true));

    let begin = tokio::select! {
        result = &mut execute => panic!("download completed before begin: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = begin
    else {
        panic!("expected download begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "download": {
                    "operation_id": operation_id,
                    "path": "/home/demo/payload.bin",
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": payload.len(),
                        "mtime_ns": "1770000000000000000",
                        "sha256": expected_hash
                    },
                    "sha256": expected_hash,
                    "byte_count": payload.len(),
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let chunk = tokio::select! {
        result = &mut execute => panic!("download completed before chunk: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = chunk
    else {
        panic!("expected download chunk request");
    };
    assert_eq!(request.payload["operation"], "download_chunk");
    journal_fault.fail_next_put();
    reply
        .send(Ok(response(
            request_id,
            json!({
                "chunk": {
                    "operation_id": operation_id,
                    "sequence": 0,
                    "offset": 0,
                    "chunk_bytes": payload,
                    "next_offset": payload.len(),
                    "eof": true
                }
            }),
        )))
        .unwrap();

    let abort = tokio::select! {
        result = &mut execute => panic!("journal failure returned before remote abort: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = abort
    else {
        panic!("expected download abort request");
    };
    assert_eq!(request.payload["operation"], "download_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Download aborted.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    let error = execute.await.unwrap_err();
    assert_eq!(error.code(), "SFTP_JOURNAL_UNAVAILABLE");
    assert!(!part.exists());
    assert!(!target.exists());
    assert!(commands.try_recv().is_err(), "journal failure was retried");
    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert!(reopened.get(operation_id).unwrap().is_none());
}

#[tokio::test]
async fn journal_actor_serializes_concurrent_calls_through_one_owner() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let actor = LocalSftpJournalActor::spawn(journal);
    let first_id = Uuid::new_v4();
    let second_id = Uuid::new_v4();

    let (first, second) = tokio::join!(
        actor.put(recovery_record(first_id)),
        actor.put(recovery_record(second_id))
    );
    first.unwrap();
    second.unwrap();

    let records = actor.list_non_terminal().await.unwrap();
    assert_eq!(records.len(), 2);
    assert!(records.iter().any(|record| record.operation_id == first_id));
    assert!(records
        .iter()
        .any(|record| record.operation_id == second_id));
}

#[test]
fn coordinator_and_journal_actor_are_sendable_across_async_workers() {
    fn assert_send<T: Send>() {}
    fn assert_send_sync<T: Send + Sync>() {}

    assert_send::<LocalSftpJournalActor>();
    assert_send_sync::<SftpCoordinator>();
}

async fn reply_to_recovery_with_reused_remote_operation(
    commands: &mut tokio::sync::mpsc::Receiver<HttpTestCommand>,
    old_operation_id: Uuid,
) {
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected recovery inspect request");
    };
    assert_eq!(request.payload["operation"], "recovery_inspect");
    reply
        .send(Ok(response(request_id, recovery_summary(old_operation_id))))
        .unwrap();

    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected recovery execute request");
    };
    assert_eq!(request.payload["operation"], "recovery_execute");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "recovery": {
                    "operation_id": old_operation_id,
                    "state": "succeeded",
                    "error_code": null,
                    "message": "Temporary file removed.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();
}

#[tokio::test]
async fn upload_preparation_blocks_every_other_mutation_until_discarded() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();

    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source.clone())),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();

    let error = coordinator.mkdir(mkdir_input()).await.unwrap_err();
    assert_eq!(error.code(), "SFTP_MUTATION_BUSY");

    coordinator
        .discard_preparation(prepared.preparation_id)
        .await
        .unwrap();
    let (mkdir, ()) = tokio::join!(
        coordinator.mkdir(mkdir_input()),
        reply_to_mkdir(&mut commands)
    );
    assert!(mkdir.is_ok());
}

#[tokio::test(start_paused = true)]
async fn preparation_expires_after_exactly_five_minutes() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();

    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source.clone())),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    assert!(open_for_write(&source).is_err());
    tokio::time::advance(Duration::from_secs(300)).await;

    let error = coordinator
        .execute_upload(prepared.preparation_id, true)
        .await
        .unwrap_err();
    assert_eq!(error.code(), "SFTP_PREPARATION_EXPIRED");
    assert!(coordinator.gates_are_free());
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    assert!(open_for_write(&source).is_ok());
}

#[tokio::test]
async fn failed_upload_preflight_releases_the_blocking_owned_source() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();
    let prepare = coordinator.prepare_upload(upload_input(source.clone()));
    let responder = async {
        let HttpTestCommand::Request { reply, .. } = commands.recv().await.unwrap() else {
            panic!("expected upload preflight request");
        };
        drop(reply);
    };

    let (result, ()) = tokio::join!(prepare, responder);
    assert_eq!(result.unwrap_err().code(), "RUNTIME_HTTP_TRANSPORT_FAILED");
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    assert!(open_for_write(&source).is_ok());
}

#[tokio::test]
async fn mutating_recovery_rejects_a_response_that_reuses_the_old_remote_operation() {
    let (coordinator, mut commands, _directory, old_operation_id) = recovery_coordinator_fixture();

    let (result, ()) = tokio::join!(
        coordinator.execute_recovery(old_operation_id, RecoveryAction::DeleteTemp, true),
        reply_to_recovery_with_reused_remote_operation(&mut commands, old_operation_id)
    );

    let error = result.unwrap_err();
    assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert!(coordinator.gates_are_free());
}

#[tokio::test]
async fn recovery_confirmation_rejection_dispatches_no_remote_request() {
    let (coordinator, mut commands, _directory, old_operation_id) = recovery_coordinator_fixture();

    assert_eq!(
        coordinator
            .execute_recovery(old_operation_id, RecoveryAction::DeleteTemp, false)
            .await
            .unwrap_err()
            .code(),
        "SFTP_CONFIRMATION_REQUIRED"
    );
    assert!(commands.try_recv().is_err());
    assert_eq!(coordinator.list_recoveries().await.unwrap().len(), 1);
}

#[tokio::test]
async fn recovery_allowlist_rejection_stops_after_read_only_inspection() {
    let (coordinator, mut commands, _directory, old_operation_id) = recovery_coordinator_fixture();
    let call =
        coordinator.execute_recovery(old_operation_id, RecoveryAction::RestoreTombstone, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery inspect request");
        };
        assert_eq!(request.payload["operation"], "recovery_inspect");
        reply
            .send(Ok(response(request_id, recovery_summary(old_operation_id))))
            .unwrap();
    };
    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(
        result.unwrap_err().code(),
        "SFTP_RECOVERY_ACTION_NOT_ALLOWED"
    );
    assert!(commands.try_recv().is_err());
}

#[tokio::test]
async fn recovery_accepts_real_python_terminal_union_and_resolves_the_local_record() {
    let (coordinator, mut commands, _directory, old_operation_id) = recovery_coordinator_fixture();
    let call = coordinator.inspect_recovery(old_operation_id);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery inspect request");
        };
        assert_eq!(request.payload["operation"], "recovery_inspect");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "recovery": {
                        "operation_id": old_operation_id,
                        "state": "failed",
                        "error_code": "SFTP_RECOVERY_TARGET_MISSING",
                        "message": "Neither target could be verified.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(call, responder);
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Terminal(terminal) = result.unwrap()
    else {
        panic!("expected real Python terminal union member");
    };
    assert_eq!(terminal.operation_id, old_operation_id);
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
}

#[tokio::test]
async fn mutating_recovery_accepts_only_a_fresh_remote_terminal_identity() {
    let (coordinator, mut commands, _directory, old_operation_id) = recovery_coordinator_fixture();
    let call = coordinator.execute_recovery(old_operation_id, RecoveryAction::DeleteTemp, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery inspect request");
        };
        assert_eq!(request.payload["operation"], "recovery_inspect");
        reply
            .send(Ok(response(request_id, recovery_summary(old_operation_id))))
            .unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery execute request");
        };
        assert_eq!(request.payload["operation"], "recovery_execute");
        assert_eq!(
            request.payload["params"]["recovery_id"],
            old_operation_id.to_string()
        );
        let new_remote_operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        assert_ne!(new_remote_operation_id, old_operation_id);
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "recovery": {
                        "operation_id": new_remote_operation_id,
                        "state": "succeeded",
                        "error_code": null,
                        "message": "Temporary file removed.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
        new_remote_operation_id
    };
    let (result, new_remote_operation_id) = tokio::join!(call, responder);
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Terminal(terminal) = result.unwrap()
    else {
        panic!("expected terminal recovery response");
    };
    assert_eq!(terminal.operation_id, new_remote_operation_id);
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
}

#[tokio::test]
async fn retained_recovery_action_restarts_and_inspects_with_its_real_remote_identity() {
    let (coordinator, mut commands, directory, old_operation_id) = recovery_coordinator_fixture();
    let call = coordinator.execute_recovery(old_operation_id, RecoveryAction::DeleteTemp, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery inspect request");
        };
        reply
            .send(Ok(response(request_id, recovery_summary(old_operation_id))))
            .unwrap();

        let HttpTestCommand::Request { request, reply, .. } = commands.recv().await.unwrap() else {
            panic!("expected recovery execute request");
        };
        let remote_operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        assert_ne!(remote_operation_id, old_operation_id);
        drop(reply);
        remote_operation_id
    };
    let (result, remote_operation_id) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    let retained = coordinator
        .list_recoveries()
        .await
        .unwrap()
        .into_iter()
        .find(|summary| summary.operation_id == remote_operation_id)
        .unwrap();
    let local_recovery_id = retained.recovery_id;
    assert_ne!(local_recovery_id, remote_operation_id);
    assert_eq!(
        retained.state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
    drop(coordinator);
    drop(commands);

    let reopened =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut restarted_commands) = runtime_http_test_channel();
    let restarted = SftpCoordinator::new(ManualSftpRuntimeClient::new(broker), reopened);
    let listed = restarted
        .list_recoveries()
        .await
        .unwrap()
        .into_iter()
        .find(|summary| summary.recovery_id == local_recovery_id)
        .unwrap();
    assert_eq!(listed.operation_id, remote_operation_id);

    let inspect = restarted.inspect_recovery(local_recovery_id);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = restarted_commands.recv().await.unwrap()
        else {
            panic!("expected restarted recovery inspect request");
        };
        assert_eq!(
            request.payload["params"]["recovery_id"],
            remote_operation_id.to_string()
        );
        reply
            .send(Ok(response(
                request_id,
                recovery_summary(remote_operation_id),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(inspect, responder);
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Summary(summary) = result.unwrap()
    else {
        panic!("expected retained recovery summary");
    };
    assert_eq!(summary.recovery_id, local_recovery_id);
    assert_eq!(summary.operation_id, remote_operation_id);
}

#[tokio::test]
async fn mutating_recovery_non_terminal_response_is_retained_as_unknown() {
    let (coordinator, mut commands, _directory, old_operation_id) = recovery_coordinator_fixture();
    let call = coordinator.execute_recovery(old_operation_id, RecoveryAction::DeleteTemp, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery inspect request");
        };
        reply
            .send(Ok(response(request_id, recovery_summary(old_operation_id))))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery execute request");
        };
        let remote_operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        reply
            .send(Ok(response(
                request_id,
                recovery_summary(remote_operation_id),
            )))
            .unwrap();
        remote_operation_id
    };
    let (result, remote_operation_id) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    let retained = coordinator
        .list_recoveries()
        .await
        .unwrap()
        .into_iter()
        .find(|summary| summary.operation_id == remote_operation_id)
        .unwrap();
    assert_eq!(
        retained.state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
}

#[tokio::test]
async fn cleanup_required_recovery_action_keeps_restart_safe_remote_identity() {
    let (coordinator, mut commands, directory, old_operation_id) = recovery_coordinator_fixture();
    let call = coordinator.execute_recovery(old_operation_id, RecoveryAction::DeleteTemp, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery inspect request");
        };
        reply
            .send(Ok(response(request_id, recovery_summary(old_operation_id))))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected recovery execute request");
        };
        let remote_operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "recovery": {
                        "operation_id": remote_operation_id,
                        "state": "cleanup_required",
                        "error_code": "SFTP_REMOTE_CLEANUP_REQUIRED",
                        "message": "Cleanup is still required.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
        remote_operation_id
    };
    let (result, remote_operation_id) = tokio::join!(call, responder);
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Terminal(terminal) = result.unwrap()
    else {
        panic!("expected cleanup-required terminal projection");
    };
    assert_eq!(terminal.operation_id, remote_operation_id);
    let retained = coordinator
        .list_recoveries()
        .await
        .unwrap()
        .into_iter()
        .find(|summary| summary.operation_id == remote_operation_id)
        .unwrap();
    assert_eq!(
        retained.state,
        harness_shell_lib::sftp::models::RecoveryState::CleanupRequired
    );
    let local_recovery_id = retained.recovery_id;
    drop(coordinator);
    drop(commands);

    let reopened =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut restarted_commands) = runtime_http_test_channel();
    let restarted = SftpCoordinator::new(ManualSftpRuntimeClient::new(broker), reopened);
    let inspect = restarted.inspect_recovery(local_recovery_id);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = restarted_commands.recv().await.unwrap()
        else {
            panic!("expected restarted recovery inspect request");
        };
        assert_eq!(
            request.payload["params"]["recovery_id"],
            remote_operation_id.to_string()
        );
        reply
            .send(Ok(response(
                request_id,
                recovery_summary(remote_operation_id),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(inspect, responder);
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Summary(summary) = result.unwrap()
    else {
        panic!("expected retained cleanup summary");
    };
    assert_eq!(summary.recovery_id, local_recovery_id);
    assert_eq!(summary.operation_id, remote_operation_id);
}

#[tokio::test]
async fn trusted_sidecar_error_is_terminal_and_does_not_create_unknown_recovery() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let call = coordinator.mkdir(mkdir_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected mkdir request");
        };
        reply
            .send(Ok(error_response(request_id, "SFTP_PERMISSION_DENIED")))
            .unwrap();
    };

    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_PERMISSION_DENIED");
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
}

#[tokio::test]
async fn inspect_entry_reads_a_symlink_target_only_after_no_follow_metadata() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let session_id = Uuid::new_v4();
    let call = coordinator.inspect_entry(session_id, "/home/demo/link");
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected lstat request");
        };
        assert_eq!(request.payload["operation"], "lstat");
        reply
            .send(Ok(response(
                request_id,
                json!({ "entry": {
                    "name": "link", "path": "/home/demo/link", "entry_type": "symlink",
                    "size": null, "mode": 41471, "mtime_ns": "1770000000000000000",
                    "link_target": null
                }}),
            )))
            .unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = tokio::time::timeout(Duration::from_millis(250), commands.recv())
            .await
            .expect("symlink inspection must issue readlink")
            .unwrap()
        else {
            panic!("expected readlink request");
        };
        assert_eq!(request.payload["operation"], "readlink");
        reply
            .send(Ok(response(
                request_id,
                json!({ "entry": {
                    "name": "link", "path": "/home/demo/link", "entry_type": "symlink",
                    "size": null, "mode": 41471, "mtime_ns": "1770000000000000000",
                    "link_target": "../target"
                }}),
            )))
            .unwrap();
    };

    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(result.unwrap().link_target.as_deref(), Some("../target"));
}

#[tokio::test]
async fn trusted_sidecar_uncertainty_keeps_the_matching_recovery_record() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let call = coordinator.mkdir(mkdir_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected mkdir request");
        };
        reply
            .send(Ok(retained_state_error_response(
                request_id,
                "SFTP_MUTATION_OUTCOME_UNKNOWN",
                "outcome_unknown",
            )))
            .unwrap();
    };

    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
}

#[tokio::test(start_paused = true)]
async fn timed_out_mutation_is_journaled_as_outcome_unknown_without_retry() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let mut call = pin!(coordinator.mkdir(mkdir_input()));
    let command = tokio::select! {
        result = &mut call => panic!("mkdir completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request { request, .. } = command else {
        panic!("expected mutation request");
    };
    assert_eq!(request.payload["operation"], "mkdir");

    tokio::time::advance(Duration::from_secs(30)).await;
    let error = call.await.unwrap_err();
    assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
    assert!(commands.try_recv().is_err());
}

#[tokio::test]
async fn dropped_mutation_reply_is_journaled_as_outcome_unknown() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let call = coordinator.mkdir(mkdir_input());
    let responder = async {
        let HttpTestCommand::Request { reply, .. } = commands.recv().await.unwrap() else {
            panic!("expected mutation request");
        };
        drop(reply);
    };
    let (result, ()) = tokio::join!(call, responder);

    let error = result.unwrap_err();
    assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
}

#[tokio::test(start_paused = true)]
async fn late_terminal_reply_converges_the_persisted_unknown_outcome() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let mut call = pin!(coordinator.mkdir(mkdir_input()));
    let command = tokio::select! {
        result = &mut call => panic!("mkdir completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = command
    else {
        panic!("expected mutation request");
    };

    tokio::time::advance(Duration::from_secs(30)).await;
    let error = call.await.unwrap_err();
    assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    let operation_id = coordinator.list_recoveries().await.unwrap()[0].operation_id;
    assert!(reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "succeeded",
                    "error_code": null,
                    "message": "Late.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .is_ok());
    // Loopback HTTP adds socket/client scheduling hops that the former in-memory
    // Protocol fixture did not have. Wait on the journal condition, not a fixed delay.
    for _ in 0..10_000 {
        if coordinator.list_recoveries().await.unwrap().is_empty() {
            return;
        }
        tokio::task::yield_now().await;
    }
    panic!(
        "late terminal response must converge the local journal: {:?}",
        coordinator.mutation_diagnostics_for_test()
    );
}

#[tokio::test]
async fn sidecar_crash_before_a_mutation_reply_keeps_a_recovery_record() {
    let (coordinator, commands, _directory) = coordinator_fixture();
    drop(commands);

    let error = coordinator.mkdir(mkdir_input()).await.unwrap_err();
    assert_eq!(error.code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
}

#[tokio::test]
async fn double_cancel_is_rejected_and_the_first_request_aborts_the_upload() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source.clone())),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let mut execute = pin!(coordinator.execute_upload(prepared.preparation_id, true));

    let command = tokio::select! {
        result = &mut execute => panic!("upload completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected upload begin request");
    };
    assert_eq!(request.payload["operation"], "upload_begin");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "upload": {
                    "operation_id": prepared.operation_id,
                    "temp_path": "/home/demo/.payload.part",
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();

    let command = tokio::select! {
        result = &mut execute => panic!("upload completed before its first chunk: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected upload chunk request");
    };
    assert_eq!(request.payload["operation"], "upload_chunk");
    assert!(coordinator.cancel(prepared.operation_id).is_ok());
    let duplicate = coordinator.cancel(prepared.operation_id).unwrap_err();
    assert_eq!(duplicate.code(), "SFTP_CANCEL_ALREADY_REQUESTED");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "chunk": {
                    "operation_id": prepared.operation_id,
                    "next_sequence": 1,
                    "next_offset": 7
                }
            }),
        )))
        .unwrap();

    let command = tokio::select! {
        result = &mut execute => panic!("upload completed without aborting: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected upload abort request");
    };
    assert_eq!(request.payload["operation"], "upload_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": prepared.operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Cancelled.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    assert_eq!(
        execute.await.unwrap().state,
        harness_shell_lib::sftp::models::OperationTerminalState::Cancelled
    );
    assert!(coordinator.gates_are_free());
}

#[tokio::test]
async fn coordinator_shutdown_discards_preparations_and_closes_permanently() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source.clone())),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    assert!(open_for_write(&source).is_err());

    let shutdown = coordinator.shutdown().await;
    assert!(shutdown.drained());
    assert!(coordinator.gates_are_free());
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    assert!(open_for_write(&source).is_ok());
    let error = coordinator
        .execute_upload(prepared.preparation_id, true)
        .await
        .unwrap_err();
    assert_eq!(error.code(), "SFTP_COORDINATOR_CLOSED");
    assert!(commands.try_recv().is_err());
}

#[tokio::test]
async fn shutdown_rejects_new_mutations_while_draining_an_active_workflow() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let mut active = pin!(coordinator.mkdir(mkdir_input()));
    let command = tokio::select! {
        result = &mut active => panic!("mkdir completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected mutation request");
    };
    let operation_id = request.payload["params"]["operation_id"]
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .unwrap();

    let mut shutdown = pin!(coordinator.shutdown());
    tokio::select! {
        result = &mut shutdown => panic!("shutdown returned before the active workflow drained: {result:?}"),
        _ = tokio::task::yield_now() => {}
    }
    let error = coordinator.mkdir(mkdir_input()).await.unwrap_err();
    assert_eq!(error.code(), "SFTP_COORDINATOR_CLOSING");
    let source = _directory.path().join("closing-payload.bin");
    fs::write(&source, b"payload").unwrap();
    let error = coordinator
        .prepare_upload(upload_input(source))
        .await
        .unwrap_err();
    assert_eq!(error.code(), "SFTP_COORDINATOR_CLOSING");
    assert!(commands.try_recv().is_err());

    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "succeeded",
                    "error_code": null,
                    "message": "Created.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();
    assert!(active.await.is_ok());
    assert!(shutdown.await.drained());

    let error = coordinator.mkdir(mkdir_input()).await.unwrap_err();
    assert_eq!(error.code(), "SFTP_COORDINATOR_CLOSED");
    let source = _directory.path().join("closed-payload.bin");
    fs::write(&source, b"payload").unwrap();
    let error = coordinator
        .prepare_upload(upload_input(source))
        .await
        .unwrap_err();
    assert_eq!(error.code(), "SFTP_COORDINATOR_CLOSED");
    assert!(commands.try_recv().is_err());
}

#[tokio::test(start_paused = true)]
async fn shutdown_has_a_bounded_drain_and_keeps_the_dispatched_journal_durable() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let mut active = pin!(coordinator.mkdir(mkdir_input()));
    let command = tokio::select! {
        result = &mut active => panic!("mkdir completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request { reply, .. } = command else {
        panic!("expected mutation request");
    };

    let (shutdown, ()) = tokio::join!(
        coordinator.shutdown(),
        tokio::time::advance(Duration::from_secs(3))
    );
    assert!(!shutdown.drained());
    assert_eq!(coordinator.list_recoveries().await.unwrap().len(), 1);
    let error = coordinator.mkdir(mkdir_input()).await.unwrap_err();
    assert_eq!(error.code(), "SFTP_COORDINATOR_CLOSED");

    drop(reply);
    tokio::task::yield_now().await;
    assert_eq!(
        active.await.unwrap_err().code(),
        "SFTP_MUTATION_OUTCOME_UNKNOWN"
    );
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
}

#[tokio::test]
async fn shutdown_requests_transfer_cancellation_before_reporting_drained() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source)),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let mut execute = pin!(coordinator.execute_upload(prepared.preparation_id, true));

    let command = tokio::select! {
        result = &mut execute => panic!("upload completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected upload begin request");
    };
    assert_eq!(request.payload["operation"], "upload_begin");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "upload": {
                    "operation_id": prepared.operation_id,
                    "temp_path": "/home/demo/.payload.part",
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();

    let command = tokio::select! {
        result = &mut execute => panic!("upload completed before its first chunk: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected upload chunk request");
    };
    assert_eq!(request.payload["operation"], "upload_chunk");

    let mut shutdown = pin!(coordinator.shutdown());
    tokio::select! {
        result = &mut shutdown => panic!("shutdown returned before transfer cancellation: {result:?}"),
        _ = tokio::task::yield_now() => {}
    }
    reply
        .send(Ok(response(
            request_id,
            json!({
                "chunk": {
                    "operation_id": prepared.operation_id,
                    "next_sequence": 1,
                    "next_offset": 7
                }
            }),
        )))
        .unwrap();

    let command = tokio::select! {
        result = &mut execute => panic!("upload completed without aborting: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected upload abort request");
    };
    assert_eq!(request.payload["operation"], "upload_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": prepared.operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Cancelled.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    assert_eq!(
        execute.await.unwrap().state,
        harness_shell_lib::sftp::models::OperationTerminalState::Cancelled
    );
    assert!(shutdown.await.drained());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn shutdown_cannot_linearize_between_mutation_check_and_dispatch_enqueue() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let dispatch_gate = MutatingDispatchTestGate::new();
    let coordinator = Arc::new(SftpCoordinator::new_with_mutating_dispatch_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        dispatch_gate.clone(),
    ));
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_upload(upload_input(source.clone())),
        reply_to_upload_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let execute_coordinator = Arc::clone(&coordinator);
    let execute = tokio::spawn(async move {
        execute_coordinator
            .execute_upload(prepared.preparation_id, true)
            .await
    });
    dispatch_gate.wait_until_blocked().await;
    assert!(commands.try_recv().is_err());

    let shutdown_coordinator = Arc::clone(&coordinator);
    let (shutdown_reply, shutdown_result) = tokio::sync::oneshot::channel();
    let shutdown = std::thread::spawn(move || {
        let outcome = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap()
            .block_on(shutdown_coordinator.shutdown());
        let _ = shutdown_reply.send(outcome);
    });
    dispatch_gate.wait_until_closing_attempted().await;
    assert!(!shutdown.is_finished());
    assert!(!dispatch_gate.closing_has_linearized());
    dispatch_gate.release();
    dispatch_gate.wait_until_closing_linearized().await;

    // The dispatch checkpoint is after cancellation check but before enqueue. Shutdown must wait
    // for that same synchronous critical section, so this already-admitted begin is enqueued first.
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = tokio::time::timeout(Duration::from_secs(5), commands.recv())
        .await
        .expect("upload begin was not enqueued before Closing")
        .unwrap()
    else {
        panic!("expected upload begin request");
    };
    assert_eq!(request.payload["operation"], "upload_begin");
    let operation_id = request.payload["params"]["operation_id"]
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .unwrap();

    let closing_error = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            match coordinator.mkdir(mkdir_input()).await {
                Err(error) if error.code() == "SFTP_COORDINATOR_CLOSING" => break error,
                Err(error) if error.code() == "SFTP_MUTATION_BUSY" => {
                    tokio::task::yield_now().await
                }
                result => panic!("shutdown did not enter Closing after the enqueue: {result:?}"),
            }
        }
    })
    .await
    .expect("shutdown did not acquire the lifecycle lock after enqueue");
    assert_eq!(closing_error.code(), "SFTP_COORDINATOR_CLOSING");
    assert!(commands.try_recv().is_err());

    reply
        .send(Ok(response(
            request_id,
            json!({
                "upload": {
                    "operation_id": operation_id,
                    "temp_path": "/home/demo/.payload.part",
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = tokio::time::timeout(Duration::from_secs(5), commands.recv())
        .await
        .expect("upload abort was not dispatched during Closing")
        .unwrap()
    else {
        panic!("expected upload abort request");
    };
    assert_eq!(request.payload["operation"], "upload_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Cancelled.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), execute)
            .await
            .expect("upload workflow did not finish after abort")
            .unwrap()
            .unwrap()
            .state,
        harness_shell_lib::sftp::models::OperationTerminalState::Cancelled
    );
    assert!(
        tokio::time::timeout(Duration::from_secs(5), shutdown_result)
            .await
            .expect("shutdown did not observe the drained workflow")
            .unwrap()
            .drained()
    );
    shutdown.join().unwrap();
    assert!(commands.try_recv().is_err());
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
    assert!(open_for_write(&source).is_ok());
}

#[tokio::test]
async fn download_non_terminal_error_closes_the_part_handle_on_the_blocking_owner() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let target = directory.path().join("download.bin");
    let payload = b"x";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let mut execute = pin!(coordinator.execute_download(prepared.preparation_id, true));

    let command = tokio::select! {
        result = &mut execute => panic!("download completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected download begin request");
    };
    assert_eq!(request.payload["operation"], "download_begin");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "download": {
                    "operation_id": prepared.operation_id,
                    "path": "/home/demo/payload.bin",
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": payload.len(),
                        "mtime_ns": "1770000000000000000",
                        "sha256": expected_hash
                    },
                    "sha256": expected_hash,
                    "byte_count": payload.len(),
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();

    let command = tokio::select! {
        result = &mut execute => panic!("download completed before its first chunk: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected download chunk request");
    };
    assert_eq!(request.payload["operation"], "download_chunk");
    let part_path = target.parent().unwrap().join(format!(
        ".harness-shell-download-{}.part",
        prepared.operation_id
    ));
    assert!(open_for_write(&part_path).is_err());
    reply
        .send(Ok(response(
            request_id,
            json!({
                "chunk": {
                    "operation_id": Uuid::new_v4(),
                    "sequence": 0,
                    "offset": 0,
                    "chunk_bytes": payload,
                    "next_offset": payload.len(),
                    "eof": true
                }
            }),
        )))
        .unwrap();

    assert_eq!(
        execute.await.unwrap_err().code(),
        "SFTP_MUTATION_OUTCOME_UNKNOWN"
    );
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    assert!(part_path.exists());
    assert!(open_for_write(&part_path).is_ok());
    fs::remove_file(part_path).unwrap();
}

#[tokio::test]
async fn dropping_a_dispatched_mkdir_future_releases_all_in_memory_owners() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let mut mkdir = Box::pin(coordinator.mkdir(mkdir_input()));
    let HttpTestCommand::Request { reply, .. } = (tokio::select! {
        result = &mut mkdir => panic!("mkdir completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    }) else {
        panic!("expected mutation request");
    };

    drop(mkdir);
    drop(reply);
    assert!(coordinator.gates_are_free());
    assert_eq!(coordinator.mutation_registration_count_for_test(), 0);
    assert!(coordinator.shutdown().await.drained());
}

#[tokio::test]
async fn dropped_caller_after_dispatch_is_durably_unknown_when_sqlite_reopens() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let mut mkdir = Box::pin(coordinator.mkdir(mkdir_input()));
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = (tokio::select! {
        result = &mut mkdir => panic!("mkdir completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    })
    else {
        panic!("expected mutation request");
    };
    let operation_id = request.payload["params"]["operation_id"]
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .unwrap();

    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "succeeded",
                    "error_code": null,
                    "message": "Created.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();
    // Let the dispatch owner deliver the reply while the coordinator future is not polled, then
    // drop that future before it can persist the terminal state.
    tokio::task::yield_now().await;
    drop(mkdir);

    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
            if reopened
                .get(operation_id)
                .unwrap()
                .is_some_and(|record| record.state == OperationState::OutcomeUnknown)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("dispatch owner did not persist OutcomeUnknown after caller drop");
}

#[tokio::test]
async fn dropped_caller_before_start_ack_is_cancelled_before_a_faulted_delete() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        fault.clone(),
    );
    // The first put is Preparing; block the actor's second put before it acknowledges dispatch.
    fault.block_put_after(1);
    let mut mkdir = Box::pin(coordinator.mkdir(mkdir_input()));
    tokio::select! {
        result = &mut mkdir => panic!("mkdir completed before start ack: {result:?}"),
        () = fault.wait_until_put_blocked() => {}
    }
    let operation_id = fault.blocked_put_operation_id().unwrap();
    drop(mkdir);
    fault.fail_next_delete();
    fault.release_put();

    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
            if reopened
                .get(operation_id)
                .unwrap()
                .is_some_and(|record| record.state == OperationState::Cancelled)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("pre-dispatch caller drop did not retain Cancelled after delete fault");
    assert!(
        commands.try_recv().is_err(),
        "runtime mutation must not start"
    );
}

#[tokio::test]
async fn dropping_freeze_reply_future_rolls_back_actor_handle_and_gate() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let reply_gate = LocalFileReplyTestGate::new();
    let coordinator = SftpCoordinator::new_with_local_file_reply_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        reply_gate.clone(),
    );
    let source = directory.path().join("payload.bin");
    fs::write(&source, b"payload").unwrap();
    let mut prepare = Box::pin(coordinator.prepare_upload(upload_input(source.clone())));

    tokio::select! {
        result = &mut prepare => panic!("prepare completed before the actor reply checkpoint: {result:?}"),
        () = reply_gate.wait_until_blocked() => {}
    }
    drop(prepare);
    assert!(coordinator.gates_are_free());
    reply_gate.release();
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    assert!(open_for_write(&source).is_ok());
    assert!(commands.try_recv().is_err());
}

#[tokio::test]
async fn dropping_create_part_reply_future_releases_actor_and_operation_owners() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let reply_gate = LocalFileReplyTestGate::new();
    let coordinator = SftpCoordinator::new_with_local_file_reply_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        reply_gate.clone(),
    );
    let target = directory.path().join("download.bin");
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight(&mut commands)
    );
    let prepared = prepared.unwrap();
    let operation_id = prepared.operation_id;
    let mut execute = Box::pin(coordinator.execute_download(prepared.preparation_id, true));
    let HttpTestCommand::Request {
        request_id, reply, ..
    } = (tokio::select! {
        result = &mut execute => panic!("download completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    })
    else {
        panic!("expected download begin request");
    };
    reply
        .send(Ok(response(
            request_id,
            json!({
                "download": {
                    "operation_id": operation_id,
                    "path": "/home/demo/payload.bin",
                    "snapshot": download_snapshot(),
                    "sha256": EMPTY_SHA256,
                    "byte_count": 0,
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();
    tokio::select! {
        result = &mut execute => panic!("download completed before the actor reply checkpoint: {result:?}"),
        () = reply_gate.wait_until_blocked() => {}
    }

    drop(execute);
    assert!(coordinator.gates_are_free());
    assert!(coordinator.progress(operation_id).is_none());
    assert_eq!(coordinator.mutation_registration_count_for_test(), 0);
    reply_gate.release();
    assert_eq!(
        coordinator.local_file_owner_count_for_test().await.unwrap(),
        0
    );
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = tokio::time::timeout(Duration::from_secs(2), commands.recv())
        .await
        .expect("dropped download did not dispatch abort")
        .unwrap()
    else {
        panic!("expected download abort request");
    };
    assert_eq!(request.payload["operation"], "download_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Download aborted.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();
    assert!(coordinator.shutdown().await.drained());
}

#[tokio::test]
async fn failed_download_part_cleanup_is_retained_as_cleanup_required() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let target = directory.path().join("download.bin");
    let payload = b"xy";
    let expected_hash = sha256(payload);
    let (prepared, ()) = tokio::join!(
        coordinator.prepare_download(download_input(target.clone())),
        reply_to_download_preflight_with(&mut commands, &expected_hash, payload.len() as u64)
    );
    let prepared = prepared.unwrap();
    let mut execute = pin!(coordinator.execute_download(prepared.preparation_id, true));

    let command = tokio::select! {
        result = &mut execute => panic!("download completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected download begin request");
    };
    assert_eq!(request.payload["operation"], "download_begin");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "download": {
                    "operation_id": prepared.operation_id,
                    "path": "/home/demo/payload.bin",
                    "snapshot": {
                        "path": "/home/demo/payload.bin",
                        "exists": true,
                        "entry_type": "file",
                        "size": payload.len(),
                        "mtime_ns": "1770000000000000000",
                        "sha256": expected_hash
                    },
                    "sha256": expected_hash,
                    "byte_count": payload.len(),
                    "next_sequence": 0,
                    "next_offset": 0
                }
            }),
        )))
        .unwrap();

    let command = tokio::select! {
        result = &mut execute => panic!("download completed before first chunk: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected download chunk request");
    };
    assert_eq!(request.payload["operation"], "download_chunk");
    let part_path = target.parent().unwrap().join(format!(
        ".harness-shell-download-{}.part",
        prepared.operation_id
    ));
    let blocker = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .open(&part_path)
        .unwrap();
    coordinator.cancel(prepared.operation_id).unwrap();
    reply
        .send(Ok(response(
            request_id,
            json!({
                "chunk": {
                    "operation_id": prepared.operation_id,
                    "sequence": 0,
                    "offset": 0,
                    "chunk_bytes": &payload[..1],
                    "next_offset": 1,
                    "eof": false
                }
            }),
        )))
        .unwrap();

    let command = tokio::select! {
        result = &mut execute => panic!("download completed without aborting: {result:?}"),
        command = commands.recv() => command.unwrap(),
    };
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = command
    else {
        panic!("expected download abort request");
    };
    assert_eq!(request.payload["operation"], "download_abort");
    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": prepared.operation_id,
                    "state": "cancelled",
                    "error_code": null,
                    "message": "Cancelled.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();

    let error = execute.await.unwrap_err();
    assert_eq!(error.code(), "SFTP_LOCAL_CLEANUP_FAILED");
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].state,
        harness_shell_lib::sftp::models::RecoveryState::CleanupRequired
    );
    drop(blocker);
    fs::remove_file(part_path).unwrap();
}

#[tokio::test]
async fn restarted_download_part_recovery_is_local_path_private_and_never_calls_python() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    // Production stores the canonical target parent selected by the native save dialog.
    let canonical_directory = fs::canonicalize(directory.path()).unwrap();
    let target_path = canonical_directory.join("downloaded-payload.bin");
    let operation_id = Uuid::new_v4();
    let part_path =
        canonical_directory.join(format!(".harness-shell-download-{operation_id}.part"));
    let payload = b"restart-safe download bytes";
    fs::write(&part_path, payload).unwrap();

    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    journal
        .put(&LocalSftpOperationRecord {
            operation_id,
            remote_operation_id: None,
            kind: OperationKind::Download,
            state: OperationState::CleanupRequired,
            connection_id: Uuid::new_v4(),
            host_label: Some("demo-host".to_owned()),
            local_path: Some(target_path.clone()),
            remote_path: "/home/demo/downloaded-payload.bin".to_owned(),
            expected_sha256: Some(sha256(payload)),
            target_snapshot: None,
            created_at: OffsetDateTime::now_utc(),
        })
        .unwrap();
    drop(journal);

    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new(ManualSftpRuntimeClient::new(broker), reopened);

    let listed = coordinator.list_recoveries().await.unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].kind, RecoveryKind::DownloadPart);
    assert_eq!(listed[0].host_label, "demo-host");
    assert_eq!(listed[0].display_name, "downloaded-payload.bin");
    assert!(listed[0]
        .available_actions
        .contains(&RecoveryAction::OpenLocalFolder));
    let encoded = serde_json::to_string(&listed[0]).unwrap();
    assert!(!encoded.contains(&target_path.to_string_lossy().to_string()));
    assert!(!encoded.contains(&part_path.to_string_lossy().to_string()));

    let inspected = tokio::time::timeout(
        Duration::from_millis(250),
        coordinator.inspect_recovery(operation_id),
    )
    .await
    .expect("local download-part inspection must not wait for Python")
    .unwrap();
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Summary(inspected) = inspected else {
        panic!("expected local recovery summary");
    };
    assert_eq!(inspected.recovery_id, operation_id);
    assert_eq!(inspected.display_name, "downloaded-payload.bin");
    assert!(commands.try_recv().is_err());

    let kept = tokio::time::timeout(
        Duration::from_millis(250),
        coordinator.execute_recovery(operation_id, RecoveryAction::Keep, true),
    )
    .await
    .expect("keeping a local download part must not wait for Python")
    .unwrap();
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Summary(kept) = kept else {
        panic!("expected retained local recovery summary");
    };
    assert_eq!(kept.recovery_id, operation_id);
    assert_eq!(coordinator.list_recoveries().await.unwrap().len(), 1);
    assert!(commands.try_recv().is_err());
}

#[tokio::test]
async fn restart_load_keeps_an_unknown_mutation_for_recovery() {
    let (coordinator, mut commands, directory) = coordinator_fixture();
    let call = coordinator.mkdir(mkdir_input());
    let responder = async {
        let HttpTestCommand::Request { reply, .. } = commands.recv().await.unwrap() else {
            panic!("expected mutation request");
        };
        drop(reply);
    };
    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    drop(coordinator);

    let reopened =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let records = reopened.list_non_terminal().unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].state, OperationState::OutcomeUnknown);
}

#[tokio::test]
async fn rename_and_remove_use_the_mutation_gate_and_exact_runtime_methods() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let source_sha256 = sha256(b"source payload");
    let source_snapshot = json!({
        "path": "/home/demo/source.txt",
        "exists": true,
        "entry_type": "file",
        "size": 14,
        "mtime_ns": "1770000000000000000",
        "sha256": source_sha256
    });
    let absent_target_snapshot = json!({
        "path": "/home/demo/target.txt",
        "exists": false,
        "entry_type": null,
        "size": null,
        "mtime_ns": null,
        "sha256": null
    });
    let rename = coordinator.rename(rename_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source lstat request");
        };
        assert_eq!(request.payload["operation"], "lstat");
        assert!(coordinator.list_recoveries().await.unwrap().is_empty());
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "entry": {
                        "name": "source.txt",
                        "path": "/home/demo/source.txt",
                        "entry_type": "file",
                        "size": 14,
                        "mode": 33188,
                        "mtime_ns": "1770000000000000000",
                        "link_target": null
                    }
                }),
            )))
            .unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source hash request");
        };
        assert_eq!(request.payload["operation"], "sha256");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "hash": {
                        "path": "/home/demo/source.txt",
                        "snapshot": source_snapshot,
                        "sha256": source_sha256,
                        "byte_count": 14
                    }
                }),
            )))
            .unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected target preflight request");
        };
        assert_eq!(request.payload["operation"], "upload_preflight");
        reply
            .send(Ok(response(
                request_id,
                json!({ "snapshot": absent_target_snapshot }),
            )))
            .unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected rename mutation request");
        };
        assert_eq!(request.payload["operation"], "rename");
        assert_eq!(
            request.payload["params"]["source_snapshot"],
            source_snapshot
        );
        assert_eq!(
            request.payload["params"]["target_snapshot"],
            absent_target_snapshot
        );
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        assert_eq!(
            coordinator.mkdir(mkdir_input()).await.unwrap_err().code(),
            "SFTP_MUTATION_BUSY"
        );
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "terminal": {
                        "operation_id": operation_id,
                        "state": "succeeded",
                        "error_code": null,
                        "message": "Renamed.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(rename, responder);
    assert_eq!(
        result.unwrap().state,
        harness_shell_lib::sftp::models::OperationTerminalState::Succeeded
    );

    let remove = coordinator.remove(remove_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected removal lstat request");
        };
        assert_eq!(request.payload["operation"], "lstat");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "entry": {
                        "name": "source.txt",
                        "path": "/home/demo/source.txt",
                        "entry_type": "file",
                        "size": 14,
                        "mode": 33188,
                        "mtime_ns": "1770000000000000000",
                        "link_target": null
                    }
                }),
            )))
            .unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected removal hash request");
        };
        assert_eq!(request.payload["operation"], "sha256");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "hash": {
                        "path": "/home/demo/source.txt",
                        "snapshot": source_snapshot,
                        "sha256": source_sha256,
                        "byte_count": 14
                    }
                }),
            )))
            .unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected remove mutation request");
        };
        assert_eq!(request.payload["operation"], "remove");
        assert_eq!(
            request.payload["params"]["expected_snapshot"],
            source_snapshot
        );
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "terminal": {
                        "operation_id": operation_id,
                        "state": "failed",
                        "error_code": "SFTP_PERMISSION_DENIED",
                        "message": "Denied.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(remove, responder);
    assert_eq!(
        result.unwrap().state,
        harness_shell_lib::sftp::models::OperationTerminalState::Failed
    );
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
}

#[tokio::test]
async fn rename_with_existing_target_requires_confirmation_without_dispatching_mutation() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let source_sha256 = sha256(b"source payload");
    let call = coordinator.rename(rename_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source lstat request");
        };
        assert_eq!(request.payload["operation"], "lstat");
        reply.send(Ok(response(request_id, json!({ "entry": {
            "name": "source.txt", "path": "/home/demo/source.txt", "entry_type": "file",
            "size": 14, "mode": 33188, "mtime_ns": "1770000000000000000", "link_target": null
        }})))).unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source hash request");
        };
        assert_eq!(request.payload["operation"], "sha256");
        reply.send(Ok(response(request_id, json!({ "hash": {
            "path": "/home/demo/source.txt", "snapshot": {
                "path": "/home/demo/source.txt", "exists": true, "entry_type": "file", "size": 14,
                "mtime_ns": "1770000000000000000", "sha256": source_sha256
            }, "sha256": source_sha256, "byte_count": 14
        }})))).unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected target preflight request");
        };
        assert_eq!(request.payload["operation"], "upload_preflight");
        reply.send(Ok(response(request_id, json!({ "snapshot": {
            "path": "/home/demo/target.txt", "exists": true, "entry_type": "file", "size": 6,
            "mtime_ns": "1770000000000000001", "sha256": sha256(b"target")
        }})))).unwrap();
    };
    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_TARGET_EXISTS");
    assert!(
        commands.try_recv().is_err(),
        "unconfirmed overwrite must not dispatch a mutation"
    );
}

#[tokio::test]
async fn confirmed_overwrite_reacquires_hashed_target_before_journal_and_dispatch() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let source_sha256 = sha256(b"source payload");
    let old_target_sha256 = sha256(b"target");
    let fresh_target_sha256 = sha256(b"target changed");
    let source_snapshot = json!({
        "path": "/home/demo/source.txt", "exists": true, "entry_type": "file", "size": 14,
        "mtime_ns": "1770000000000000000", "sha256": source_sha256
    });
    let fresh_target_snapshot = json!({
        "path": "/home/demo/target.txt", "exists": true, "entry_type": "file", "size": 14,
        "mtime_ns": "1770000000000000002", "sha256": fresh_target_sha256
    });

    let first_attempt = coordinator.rename(rename_input());
    let first_responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected initial source lstat request");
        };
        assert_eq!(request.payload["operation"], "lstat");
        reply.send(Ok(response(request_id, json!({ "entry": {
            "name": "source.txt", "path": "/home/demo/source.txt", "entry_type": "file",
            "size": 14, "mode": 33188, "mtime_ns": "1770000000000000000", "link_target": null
        }})))).unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected initial source hash request");
        };
        assert_eq!(request.payload["operation"], "sha256");
        reply
            .send(Ok(response(
                request_id,
                json!({ "hash": {
                    "path": "/home/demo/source.txt", "snapshot": source_snapshot,
                    "sha256": source_sha256, "byte_count": 14
                }}),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected initial target preflight request");
        };
        assert_eq!(request.payload["operation"], "upload_preflight");
        reply.send(Ok(response(request_id, json!({ "snapshot": {
            "path": "/home/demo/target.txt", "exists": true, "entry_type": "file", "size": 6,
            "mtime_ns": "1770000000000000001", "sha256": old_target_sha256
        }})))).unwrap();
    };
    let (first_result, ()) = tokio::join!(first_attempt, first_responder);
    assert_eq!(first_result.unwrap_err().code(), "SFTP_TARGET_EXISTS");

    let confirmed_attempt = coordinator.rename(overwrite_rename_input());
    let confirmed_responder =
        async {
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected confirmed source lstat request");
            };
            assert_eq!(request.payload["operation"], "lstat");
            reply.send(Ok(response(request_id, json!({ "entry": {
            "name": "source.txt", "path": "/home/demo/source.txt", "entry_type": "file",
            "size": 14, "mode": 33188, "mtime_ns": "1770000000000000000", "link_target": null
        }})))).unwrap();
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected confirmed source hash request");
            };
            assert_eq!(request.payload["operation"], "sha256");
            reply
                .send(Ok(response(
                    request_id,
                    json!({ "hash": {
                        "path": "/home/demo/source.txt", "snapshot": source_snapshot,
                        "sha256": source_sha256, "byte_count": 14
                    }}),
                )))
                .unwrap();
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected fresh confirmed target preflight request");
            };
            assert_eq!(request.payload["operation"], "upload_preflight");
            reply
                .send(Ok(response(
                    request_id,
                    json!({ "snapshot": fresh_target_snapshot }),
                )))
                .unwrap();
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected fresh confirmed target hash request");
            };
            assert_eq!(request.payload["operation"], "sha256");
            reply
                .send(Ok(response(
                    request_id,
                    json!({ "hash": {
                        "path": "/home/demo/target.txt", "snapshot": fresh_target_snapshot,
                        "sha256": fresh_target_sha256, "byte_count": 14
                    }}),
                )))
                .unwrap();
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = tokio::time::timeout(Duration::from_secs(1), commands.recv())
                .await
                .expect("confirmed target verification must lead to one rename dispatch")
                .unwrap()
            else {
                panic!("expected confirmed rename dispatch");
            };
            assert_eq!(request.payload["operation"], "rename");
            assert_eq!(
                request.payload["params"]["target_snapshot"],
                fresh_target_snapshot
            );
            let operation_id = request.payload["params"]["operation_id"]
                .as_str()
                .and_then(|value| Uuid::parse_str(value).ok())
                .unwrap();
            reply.send(Ok(response(request_id, json!({ "terminal": {
            "operation_id": operation_id, "state": "succeeded", "error_code": null,
            "message": "Renamed.", "sha256": null, "byte_count": null, "recovery_id": null
        }})))).unwrap();
        };
    let (confirmed_result, ()) = tokio::join!(confirmed_attempt, confirmed_responder);
    assert_eq!(
        confirmed_result.unwrap().state,
        harness_shell_lib::sftp::models::OperationTerminalState::Succeeded
    );
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
}

#[tokio::test]
async fn rename_rejects_hash_path_and_metadata_mismatches_before_dispatch() {
    for case in ["path", "metadata", "inner_outer", "byte_count"] {
        let (coordinator, mut commands, _directory) = coordinator_fixture();
        let source_sha256 = sha256(b"source payload");
        let call = coordinator.rename(rename_input());
        let responder = async {
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected source lstat request");
            };
            assert_eq!(request.payload["operation"], "lstat");
            reply.send(Ok(response(request_id, json!({ "entry": {
                "name": "source.txt", "path": "/home/demo/source.txt", "entry_type": "file",
                "size": 14, "mode": 33188, "mtime_ns": "1770000000000000000", "link_target": null
            }})))).unwrap();
            let HttpTestCommand::Request {
                request_id,
                request,
                reply,
            } = commands.recv().await.unwrap()
            else {
                panic!("expected source hash request");
            };
            assert_eq!(request.payload["operation"], "sha256");
            let hash_path = if case == "path" {
                "/home/demo/other.txt"
            } else {
                "/home/demo/source.txt"
            };
            let snapshot_mtime = if case == "metadata" {
                "1770000000000000001"
            } else {
                "1770000000000000000"
            };
            let snapshot_sha256 = if case == "inner_outer" {
                sha256(b"a different inner hash")
            } else {
                source_sha256.clone()
            };
            let byte_count = if case == "byte_count" { 13 } else { 14 };
            reply.send(Ok(response(request_id, json!({ "hash": {
                "path": hash_path,
                "snapshot": {
                    "path": "/home/demo/source.txt", "exists": true, "entry_type": "file", "size": 14,
                    "mtime_ns": snapshot_mtime, "sha256": snapshot_sha256
                },
                "sha256": source_sha256, "byte_count": byte_count
            }})))).unwrap();
        };
        let (result, ()) = tokio::join!(call, responder);
        assert_eq!(result.unwrap_err().code(), "SIDECAR_RESPONSE_INVALID");
        assert!(
            commands.try_recv().is_err(),
            "invalid hash evidence must not dispatch mutation"
        );
    }
}

#[tokio::test]
async fn rename_rejects_hash_that_disagrees_with_hashed_target_preflight() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let source_sha256 = sha256(b"source payload");
    let preflight_sha256 = sha256(b"target before");
    let response_sha256 = sha256(b"target after!");
    let call = coordinator.rename(overwrite_rename_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source lstat request");
        };
        assert_eq!(request.payload["operation"], "lstat");
        reply
            .send(Ok(response(
                request_id,
                json!({ "entry": {
                    "name": "source.txt", "path": "/home/demo/source.txt", "entry_type": "file",
                    "size": 14, "mode": 33188, "mtime_ns": "1770000000000000000", "link_target": null
                }}),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source hash request");
        };
        assert_eq!(request.payload["operation"], "sha256");
        reply
            .send(Ok(response(
                request_id,
                json!({ "hash": {
                    "path": "/home/demo/source.txt",
                    "snapshot": {
                        "path": "/home/demo/source.txt", "exists": true, "entry_type": "file", "size": 14,
                        "mtime_ns": "1770000000000000000", "sha256": source_sha256
                    },
                    "sha256": source_sha256, "byte_count": 14
                }}),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected target preflight request");
        };
        assert_eq!(request.payload["operation"], "upload_preflight");
        reply
            .send(Ok(response(
                request_id,
                json!({ "snapshot": {
                    "path": "/home/demo/target.txt", "exists": true, "entry_type": "file", "size": 13,
                    "mtime_ns": "1770000000000000001", "sha256": preflight_sha256
                }}),
            )))
            .unwrap();
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected target hash request");
        };
        assert_eq!(request.payload["operation"], "sha256");
        reply
            .send(Ok(response(
                request_id,
                json!({ "hash": {
                    "path": "/home/demo/target.txt",
                    "snapshot": {
                        "path": "/home/demo/target.txt", "exists": true, "entry_type": "file", "size": 13,
                        "mtime_ns": "1770000000000000001", "sha256": response_sha256
                    },
                    "sha256": response_sha256, "byte_count": 13
                }}),
            )))
            .unwrap();
        if let Ok(Some(HttpTestCommand::Request { reply, .. })) =
            tokio::time::timeout(Duration::from_secs(1), commands.recv()).await
        {
            // The pre-fix coordinator dispatches this untrusted replacement. Dropping the
            // reply resolves that bad path without inventing a successful remote mutation.
            drop(reply);
        }
    };
    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SIDECAR_RESPONSE_INVALID");
}

#[tokio::test]
async fn dropped_rename_reply_is_unknown_and_is_never_retried() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let source_sha256 = sha256(b"source payload");
    let call = coordinator.rename(rename_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source lstat request");
        };
        assert_eq!(request.payload["operation"], "lstat");
        reply.send(Ok(response(request_id, json!({ "entry": {
            "name": "source.txt", "path": "/home/demo/source.txt", "entry_type": "file",
            "size": 14, "mode": 33188, "mtime_ns": "1770000000000000000", "link_target": null
        }})))).unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected source hash request");
        };
        assert_eq!(request.payload["operation"], "sha256");
        reply.send(Ok(response(request_id, json!({ "hash": {
            "path": "/home/demo/source.txt", "snapshot": {
                "path": "/home/demo/source.txt", "exists": true, "entry_type": "file", "size": 14,
                "mtime_ns": "1770000000000000000", "sha256": source_sha256
            }, "sha256": source_sha256, "byte_count": 14
        }})))).unwrap();

        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected target preflight request");
        };
        assert_eq!(request.payload["operation"], "upload_preflight");
        reply.send(Ok(response(request_id, json!({ "snapshot": {
            "path": "/home/demo/target.txt", "exists": false, "entry_type": null, "size": null,
            "mtime_ns": null, "sha256": null
        }})))).unwrap();

        let HttpTestCommand::Request { request, reply, .. } = commands.recv().await.unwrap() else {
            panic!("expected rename mutation request");
        };
        assert_eq!(request.payload["operation"], "rename");
        drop(reply);
    };
    let (result, ()) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].state,
        harness_shell_lib::sftp::models::RecoveryState::OutcomeUnknown
    );
    assert!(commands.try_recv().is_err(), "rename must not be replayed");
}

#[tokio::test]
async fn recursive_delete_preflight_is_one_shot_and_execute_is_journaled_before_dispatch() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let delete_plan_id = Uuid::new_v4();
    let preflight = coordinator.preflight_delete(delete_preflight_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected delete preflight request");
        };
        assert_eq!(request.payload["operation"], "delete_preflight");
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        assert_eq!(coordinator.list_recoveries().await.unwrap().len(), 1);
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "delete_plan": {
                        "delete_plan_id": delete_plan_id,
                        "operation_id": operation_id,
                        "root_path": "/home/demo/tree",
                        "root_snapshot": {
                            "path": "/home/demo/tree",
                            "exists": true,
                            "entry_type": "directory",
                            "size": null,
                            "mtime_ns": "1770000000000000000",
                            "sha256": null
                        },
                        "file_count": 2,
                        "directory_count": 1,
                        "symlink_count": 0,
                        "total_byte_count": 12,
                        "manifest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "complete": true
                    }
                }),
            )))
            .unwrap();
        operation_id
    };
    let (plan, operation_id) = tokio::join!(preflight, responder);
    assert_eq!(plan.unwrap().operation_id, operation_id);
    assert!(coordinator.gates_are_free());

    let execute = coordinator.execute_delete(delete_plan_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected delete execute request");
        };
        assert_eq!(request.payload["operation"], "delete_execute");
        assert_eq!(
            request.payload["params"]["delete_plan_id"],
            delete_plan_id.to_string()
        );
        let recoveries = coordinator.list_recoveries().await.unwrap();
        assert_eq!(
            recoveries.len(),
            1,
            "journal must exist before mutation dispatch"
        );
        assert_eq!(recoveries[0].operation_id, operation_id);
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "terminal": {
                        "operation_id": operation_id,
                        "state": "succeeded",
                        "error_code": null,
                        "message": "Deleted.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(execute, responder);
    assert_eq!(result.unwrap().operation_id, operation_id);
    assert!(coordinator.list_recoveries().await.unwrap().is_empty());
    assert_eq!(
        coordinator
            .execute_delete(delete_plan_id, true)
            .await
            .unwrap_err()
            .code(),
        "SFTP_DELETE_PLAN_NOT_FOUND"
    );
}

#[tokio::test]
async fn recursive_delete_execute_journal_failure_keeps_the_plan_retryable() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        fault.clone(),
    );
    let delete_plan_id = Uuid::new_v4();
    let preflight = coordinator.preflight_delete(delete_preflight_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected delete preflight request");
        };
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "delete_plan": {
                        "delete_plan_id": delete_plan_id,
                        "operation_id": operation_id,
                        "root_path": "/home/demo/tree",
                        "root_snapshot": {
                            "path": "/home/demo/tree",
                            "exists": true,
                            "entry_type": "directory",
                            "size": null,
                            "mtime_ns": "1770000000000000000",
                            "sha256": null
                        },
                        "file_count": 0,
                        "directory_count": 1,
                        "symlink_count": 0,
                        "total_byte_count": 0,
                        "manifest_sha256": EMPTY_SHA256,
                        "complete": true
                    }
                }),
            )))
            .unwrap();
        operation_id
    };
    let (plan, operation_id) = tokio::join!(preflight, responder);
    assert_eq!(plan.unwrap().operation_id, operation_id);

    fault.fail_next_put();
    assert_eq!(
        coordinator
            .execute_delete(delete_plan_id, true)
            .await
            .unwrap_err()
            .code(),
        "SFTP_JOURNAL_UNAVAILABLE"
    );
    assert_eq!(
        coordinator.delete_preparation_count_for_test(),
        1,
        "journal readiness failure must not consume the in-memory plan"
    );

    let retry = coordinator.execute_delete(delete_plan_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected retry delete execute request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "terminal": {
                        "operation_id": operation_id,
                        "state": "succeeded",
                        "error_code": null,
                        "message": "Deleted.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(retry, responder);
    assert_eq!(result.unwrap().operation_id, operation_id);
}

#[tokio::test]
async fn recursive_delete_preflight_journal_failure_dispatches_no_remote_plan() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        fault.clone(),
    );
    fault.fail_next_put();

    assert_eq!(
        coordinator
            .preflight_delete(delete_preflight_input())
            .await
            .unwrap_err()
            .code(),
        "SFTP_JOURNAL_UNAVAILABLE"
    );
    assert!(
        commands.try_recv().is_err(),
        "Python must not persist a delete plan before the local identity is durable"
    );
    assert!(coordinator.gates_are_free());
}

#[tokio::test]
async fn recursive_delete_execute_busy_keeps_the_plan_retryable() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    let (delete_plan_id, operation_id) =
        complete_recursive_delete_preflight(&coordinator, &mut commands).await;

    let mut mkdir = pin!(coordinator.mkdir(mkdir_input()));
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = (tokio::select! {
        result = &mut mkdir => panic!("mkdir completed unexpectedly: {result:?}"),
        command = commands.recv() => command.unwrap(),
    })
    else {
        panic!("expected mkdir request");
    };
    let mkdir_operation_id = request.payload["params"]["operation_id"]
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .unwrap();

    assert_eq!(
        coordinator
            .execute_delete(delete_plan_id, true)
            .await
            .unwrap_err()
            .code(),
        "SFTP_MUTATION_BUSY"
    );
    assert_eq!(coordinator.delete_preparation_count_for_test(), 1);

    reply
        .send(Ok(response(
            request_id,
            json!({
                "terminal": {
                    "operation_id": mkdir_operation_id,
                    "state": "succeeded",
                    "error_code": null,
                    "message": "Created.",
                    "sha256": null,
                    "byte_count": null,
                    "recovery_id": null
                }
            }),
        )))
        .unwrap();
    assert!(mkdir.await.is_ok());

    let retry = coordinator.execute_delete(delete_plan_id, true);
    let responder = async {
        let HttpTestCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.unwrap()
        else {
            panic!("expected retry delete execute request");
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "terminal": {
                        "operation_id": operation_id,
                        "state": "succeeded",
                        "error_code": null,
                        "message": "Deleted.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
    };
    let (result, ()) = tokio::join!(retry, responder);
    assert_eq!(result.unwrap().operation_id, operation_id);
}

#[tokio::test]
async fn recursive_delete_preflight_reply_loss_restarts_with_the_same_remote_identity() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new(ManualSftpRuntimeClient::new(broker), journal);

    let preflight = coordinator.preflight_delete(delete_preflight_input());
    let responder = async {
        let HttpTestCommand::Request { request, reply, .. } = commands.recv().await.unwrap() else {
            panic!("expected delete preflight request");
        };
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        drop(reply);
        operation_id
    };
    let (result, operation_id) = tokio::join!(preflight, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert_eq!(
        coordinator.list_recoveries().await.unwrap()[0].operation_id,
        operation_id
    );
    drop(coordinator);
    drop(commands);

    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let (broker, mut restarted_commands) = runtime_http_test_channel();
    let restarted = SftpCoordinator::new(ManualSftpRuntimeClient::new(broker), reopened);
    let recovery_id = restarted.list_recoveries().await.unwrap()[0].recovery_id;
    assert_eq!(recovery_id, operation_id);
    let inspect = restarted.inspect_recovery(recovery_id);
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = restarted_commands.recv().await.unwrap()
        else {
            panic!("expected restarted recovery inspect request");
        };
        assert_eq!(
            request.payload["params"]["recovery_id"],
            operation_id.to_string()
        );
        reply
            .send(Ok(response(request_id, recovery_summary(operation_id))))
            .unwrap();
    };
    let (result, ()) = tokio::join!(inspect, responder);
    let harness_shell_lib::sftp::protocol::RecoveryResponse::Summary(summary) = result.unwrap()
    else {
        panic!("expected recovery summary");
    };
    assert_eq!(summary.recovery_id, operation_id);
    assert_eq!(summary.operation_id, operation_id);
}

#[tokio::test]
async fn delete_preflight_cannot_publish_a_plan_after_shutdown_linearizes() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let (broker, mut commands) = runtime_http_test_channel();
    let dispatch_gate = MutatingDispatchTestGate::new();
    dispatch_gate.release();
    let coordinator = Arc::new(SftpCoordinator::new_with_mutating_dispatch_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        dispatch_gate.clone(),
    ));
    let preflight_coordinator = Arc::clone(&coordinator);
    let preflight = tokio::spawn(async move {
        preflight_coordinator
            .preflight_delete(delete_preflight_input())
            .await
    });
    let HttpTestCommand::Request {
        request_id,
        request,
        reply,
    } = commands.recv().await.unwrap()
    else {
        panic!("expected delete preflight request");
    };

    let shutdown_coordinator = Arc::clone(&coordinator);
    let shutdown = tokio::spawn(async move { shutdown_coordinator.shutdown().await });
    dispatch_gate.wait_until_closing_linearized().await;
    let delete_plan_id = Uuid::new_v4();
    let operation_id = request.payload["params"]["operation_id"]
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .unwrap();
    reply
        .send(Ok(response(
            request_id,
            json!({
                "delete_plan": {
                    "delete_plan_id": delete_plan_id,
                    "operation_id": operation_id,
                    "root_path": "/home/demo/tree",
                    "root_snapshot": {
                        "path": "/home/demo/tree",
                        "exists": true,
                        "entry_type": "directory",
                        "size": null,
                        "mtime_ns": "1770000000000000000",
                        "sha256": null
                    },
                    "file_count": 0,
                    "directory_count": 1,
                    "symlink_count": 0,
                    "total_byte_count": 0,
                    "manifest_sha256": EMPTY_SHA256,
                    "complete": true
                }
            }),
        )))
        .unwrap();

    assert_eq!(
        preflight.await.unwrap().unwrap_err().code(),
        "SFTP_COORDINATOR_CLOSING"
    );
    assert_eq!(coordinator.delete_preparation_count_for_test(), 0);
    assert!(shutdown.await.unwrap().drained());
    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert_eq!(
        reopened.get(operation_id).unwrap().unwrap().operation_id,
        operation_id,
        "shutdown must not orphan the Sidecar plan identity"
    );
}

#[tokio::test]
async fn delete_preflight_rejects_each_cross_field_identity_and_root_invariant() {
    let (coordinator, mut commands, _directory) = coordinator_fixture();
    for case in ["snapshot_path", "missing_root", "not_directory", "same_ids"] {
        let delete_plan_id = Uuid::new_v4();
        let operation_id = if case == "same_ids" {
            delete_plan_id
        } else {
            Uuid::new_v4()
        };
        let mut plan = json!({
            "delete_plan": {
                "delete_plan_id": delete_plan_id,
                "operation_id": operation_id,
                "root_path": "/home/demo/tree",
                "root_snapshot": {
                    "path": "/home/demo/tree",
                    "exists": true,
                    "entry_type": "directory",
                    "size": null,
                    "mtime_ns": "1770000000000000000",
                    "sha256": null
                },
                "file_count": 0,
                "directory_count": 1,
                "symlink_count": 0,
                "total_byte_count": 0,
                "manifest_sha256": EMPTY_SHA256,
                "complete": true
            }
        });
        match case {
            "snapshot_path" => {
                plan["delete_plan"]["root_snapshot"]["path"] = json!("/home/demo/other")
            }
            "missing_root" => plan["delete_plan"]["root_snapshot"]["exists"] = json!(false),
            "not_directory" => plan["delete_plan"]["root_snapshot"]["entry_type"] = json!("file"),
            "same_ids" => {}
            _ => unreachable!(),
        }
        let preflight = coordinator.preflight_delete(delete_preflight_input());
        let responder = async {
            let HttpTestCommand::Request {
                request_id, reply, ..
            } = commands.recv().await.unwrap()
            else {
                panic!("expected delete preflight request");
            };
            reply.send(Ok(response(request_id, plan))).unwrap();
        };
        let (result, ()) = tokio::join!(preflight, responder);
        assert_eq!(result.unwrap_err().code(), "SIDECAR_RESPONSE_INVALID");
        assert_eq!(coordinator.delete_preparation_count_for_test(), 0);
    }
}

#[tokio::test]
async fn terminal_record_remains_durable_when_journal_delete_fails() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        fault.clone(),
    );
    fault.fail_next_delete();
    let call = coordinator.mkdir(mkdir_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected mkdir request");
        };
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "terminal": {
                        "operation_id": operation_id,
                        "state": "failed",
                        "error_code": "SFTP_PERMISSION_DENIED",
                        "message": "Denied.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
        operation_id
    };
    let (result, operation_id) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_JOURNAL_UNAVAILABLE");
    drop(coordinator);
    drop(commands);

    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert_eq!(
        reopened.get(operation_id).unwrap().unwrap().state,
        OperationState::Failed,
        "terminal put must happen before the failed delete"
    );
}

#[tokio::test]
async fn terminal_record_remains_durable_when_journal_delete_returns_false() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        fault.clone(),
    );
    fault.return_false_next_delete();
    let call = coordinator.mkdir(mkdir_input());
    let responder = async {
        let HttpTestCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected mkdir request");
        };
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "terminal": {
                        "operation_id": operation_id,
                        "state": "failed",
                        "error_code": "SFTP_PERMISSION_DENIED",
                        "message": "Denied.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
        operation_id
    };
    let (result, operation_id) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_JOURNAL_INVARIANT");
    drop(coordinator);
    drop(commands);

    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert_eq!(
        reopened.get(operation_id).unwrap().unwrap().state,
        OperationState::Failed,
        "a false delete result must not hide the durable terminal record"
    );
}

#[tokio::test]
async fn enqueue_rejection_persists_cancelled_before_a_faulted_delete() {
    let directory = tempfile::tempdir().unwrap();
    let journal_path = directory.path().join("manual-sftp.sqlite3");
    let journal = LocalSftpOperationJournal::open(&journal_path).unwrap();
    let journal_fault = JournalFaultTestGate::new();
    let dispatch_gate = MutatingDispatchTestGate::new();
    let (broker, _commands) = runtime_http_test_channel();
    let coordinator = Arc::new(SftpCoordinator::new_with_dispatch_and_journal_test_gates(
        ManualSftpRuntimeClient::new(broker),
        journal,
        dispatch_gate.clone(),
        journal_fault.clone(),
    ));
    journal_fault.block_next_put();
    let mkdir_coordinator = Arc::clone(&coordinator);
    let mkdir = tokio::spawn(async move { mkdir_coordinator.mkdir(mkdir_input()).await });
    journal_fault.wait_until_put_blocked().await;
    let operation_id = journal_fault.blocked_put_operation_id().unwrap();

    let shutdown_coordinator = Arc::clone(&coordinator);
    let shutdown = tokio::spawn(async move { shutdown_coordinator.shutdown().await });
    dispatch_gate.wait_until_closing_linearized().await;
    journal_fault.fail_next_delete();
    journal_fault.release_put();

    let result = mkdir.await.unwrap();
    assert_eq!(result.unwrap_err().code(), "SFTP_JOURNAL_UNAVAILABLE");
    assert!(shutdown.await.unwrap().drained());
    drop(coordinator);

    let reopened = LocalSftpOperationJournal::open(&journal_path).unwrap();
    assert_eq!(
        reopened.get(operation_id).unwrap().unwrap().state,
        OperationState::Cancelled
    );
}

#[tokio::test]
async fn unknown_journal_transition_failure_keeps_public_code_and_safe_diagnostic() {
    let directory = tempfile::tempdir().unwrap();
    let journal =
        LocalSftpOperationJournal::open(&directory.path().join("manual-sftp.sqlite3")).unwrap();
    let fault = JournalFaultTestGate::new();
    let (broker, mut commands) = runtime_http_test_channel();
    let coordinator = SftpCoordinator::new_with_journal_fault_test_gate(
        ManualSftpRuntimeClient::new(broker),
        journal,
        fault.clone(),
    );
    let call = coordinator.mkdir(mkdir_input());
    let responder = async {
        let HttpTestCommand::Request { request, reply, .. } = commands.recv().await.unwrap() else {
            panic!("expected mkdir request");
        };
        let operation_id = request.payload["params"]["operation_id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap();
        fault.fail_next_put();
        drop(reply);
        operation_id
    };
    let (result, operation_id) = tokio::join!(call, responder);
    assert_eq!(result.unwrap_err().code(), "SFTP_MUTATION_OUTCOME_UNKNOWN");
    assert_eq!(
        coordinator.mutation_diagnostics_for_test(),
        vec![harness_shell_lib::sftp::coordinator::MutationDiagnostic {
            operation_id,
            cause_code: "RUNTIME_HTTP_TRANSPORT_FAILED".to_owned(),
            journal_error_code: Some("SFTP_JOURNAL_UNAVAILABLE".to_owned()),
        }]
    );
}
