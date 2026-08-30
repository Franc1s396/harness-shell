use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::{
    protocol::MessageType,
    sidecar::broker::{RuntimeBrokerHandle, RuntimeRequest},
};

use super::models::{
    DeletePlanSummary, DownloadChunk, DownloadReady, ListingBatch, ManualSftpContext,
    ManualSftpError, OperationTerminalProjection, RecoveryAction, RecoverySummary, RemoteEntry,
    RemoteFileHash, RetainedOperationState, TransferSnapshot, UploadChunkAck, UploadReady,
    SFTP_CHUNK_BYTES, SFTP_SEQUENCE_MAX,
};

/// Owns the exact Protocol v1 method names and strict result decoding for manual SFTP.
#[derive(Clone)]
pub struct ManualSftpRuntimeClient {
    broker: RuntimeBrokerHandle,
}

impl ManualSftpRuntimeClient {
    pub fn new(broker: RuntimeBrokerHandle) -> Self {
        Self { broker }
    }

    pub async fn open(&self, ssh_session_id: Uuid) -> Result<ManualSftpContext, ManualSftpError> {
        self.call::<ContextResult, _>("manual_sftp.open", &SessionParams { ssh_session_id }, false)
            .await
            .map(|result| result.context)
    }

    pub async fn list_begin(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<ListingBatch, ManualSftpError> {
        self.call::<BatchResult, _>(
            "manual_sftp.list.begin",
            &SessionPathParams {
                ssh_session_id,
                path,
            },
            false,
        )
        .await
        .map(|result| result.batch)
    }

    pub async fn list_next(
        &self,
        listing_id: Uuid,
        sequence: u32,
    ) -> Result<ListingBatch, ManualSftpError> {
        require_sequence(sequence)?;
        self.call::<BatchResult, _>(
            "manual_sftp.list.next",
            &ListingNextParams {
                listing_id,
                sequence,
            },
            false,
        )
        .await
        .map(|result| result.batch)
    }

    pub async fn list_close(&self, listing_id: Uuid) -> Result<bool, ManualSftpError> {
        self.call::<ClosedResult, _>(
            "manual_sftp.list.close",
            &ListingCloseParams { listing_id },
            false,
        )
        .await
        .map(|result| result.closed)
    }

    pub async fn lstat(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteEntry, ManualSftpError> {
        self.entry_call("manual_sftp.lstat", ssh_session_id, path)
            .await
    }

    pub async fn readlink(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteEntry, ManualSftpError> {
        self.entry_call("manual_sftp.readlink", ssh_session_id, path)
            .await
    }

    pub async fn realpath(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteEntry, ManualSftpError> {
        self.entry_call("manual_sftp.realpath", ssh_session_id, path)
            .await
    }

    pub async fn sha256(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteFileHash, ManualSftpError> {
        self.call::<HashResult, _>(
            "manual_sftp.sha256",
            &SessionPathParams {
                ssh_session_id,
                path,
            },
            false,
        )
        .await
        .map(|result| result.hash)
    }

    pub async fn upload_preflight(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<TransferSnapshot, ManualSftpError> {
        self.call::<SnapshotResult, _>(
            "manual_sftp.upload.preflight",
            &SessionPathParams {
                ssh_session_id,
                path,
            },
            false,
        )
        .await
        .map(|result| result.snapshot)
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn upload_begin(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: &str,
        source_sha256: &str,
        source_byte_count: u64,
        target_snapshot: &TransferSnapshot,
    ) -> Result<UploadReady, ManualSftpError> {
        super::models::require_js_safe(source_byte_count)?;
        self.call::<UploadResult, _>(
            "manual_sftp.upload.begin",
            &UploadBeginParams {
                operation_id,
                ssh_session_id,
                path,
                source_sha256,
                source_byte_count,
                target_snapshot,
            },
            false,
        )
        .await
        .map(|result| result.upload)
    }

    pub async fn upload_chunk(
        &self,
        operation_id: Uuid,
        sequence: u32,
        offset: u64,
        chunk: &[u8],
    ) -> Result<UploadChunkAck, ManualSftpError> {
        require_sequence(sequence)?;
        super::models::require_js_safe(offset)?;
        if chunk.len() > SFTP_CHUNK_BYTES {
            return Err(ManualSftpError::new(
                "SFTP_CHUNK_LIMIT_EXCEEDED",
                "The transfer chunk exceeds the supported size.",
            ));
        }
        let chunk_b64 = STANDARD.encode(chunk);
        self.call::<UploadChunkResult, _>(
            "manual_sftp.upload.chunk",
            &UploadChunkParams {
                operation_id,
                sequence,
                offset,
                chunk_b64: &chunk_b64,
            },
            true,
        )
        .await
        .map(|result| result.chunk)
    }

    pub async fn upload_finish(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.terminal_call("manual_sftp.upload.finish", operation_id)
            .await
    }

    pub async fn upload_abort(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.terminal_call("manual_sftp.upload.abort", operation_id)
            .await
    }

    pub async fn download_begin(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<DownloadReady, ManualSftpError> {
        self.call::<DownloadResult, _>(
            "manual_sftp.download.begin",
            &DownloadBeginParams {
                operation_id,
                ssh_session_id,
                path,
            },
            false,
        )
        .await
        .map(|result| result.download)
    }

    pub async fn download_chunk(
        &self,
        operation_id: Uuid,
        sequence: u32,
        offset: u64,
    ) -> Result<DownloadChunk, ManualSftpError> {
        require_sequence(sequence)?;
        super::models::require_js_safe(offset)?;
        self.call::<DownloadChunkResult, _>(
            "manual_sftp.download.chunk",
            &DownloadChunkParams {
                operation_id,
                sequence,
                offset,
            },
            true,
        )
        .await
        .map(|result| result.chunk)
    }

    pub async fn download_finish(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.terminal_call("manual_sftp.download.finish", operation_id)
            .await
    }

    pub async fn download_abort(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.terminal_call("manual_sftp.download.abort", operation_id)
            .await
    }

    pub async fn mkdir(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        parent_path: &str,
        name: &str,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.call::<TerminalResult, _>(
            "manual_sftp.mkdir",
            &MkdirParams {
                operation_id,
                ssh_session_id,
                parent_path,
                name,
            },
            false,
        )
        .await
        .map(|result| result.terminal)
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn rename(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        source_path: &str,
        target_path: &str,
        overwrite: bool,
        source_snapshot: Option<&TransferSnapshot>,
        target_snapshot: Option<&TransferSnapshot>,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.call::<TerminalResult, _>(
            "manual_sftp.rename",
            &RenameParams {
                operation_id,
                ssh_session_id,
                source_path,
                target_path,
                overwrite,
                source_snapshot,
                target_snapshot,
            },
            false,
        )
        .await
        .map(|result| result.terminal)
    }

    pub async fn remove(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: &str,
        expected_snapshot: &TransferSnapshot,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.call::<TerminalResult, _>(
            "manual_sftp.remove",
            &RemoveParams {
                operation_id,
                ssh_session_id,
                path,
                expected_snapshot,
            },
            false,
        )
        .await
        .map(|result| result.terminal)
    }

    pub async fn delete_preflight(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<DeletePlanSummary, ManualSftpError> {
        self.call::<DeletePlanResult, _>(
            "manual_sftp.delete.preflight",
            &DeletePreflightParams {
                operation_id,
                ssh_session_id,
                path,
            },
            false,
        )
        .await
        .map(|result| result.delete_plan)
    }

    pub async fn delete_execute(
        &self,
        delete_plan_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.call::<TerminalResult, _>(
            "manual_sftp.delete.execute",
            &DeleteExecuteParams { delete_plan_id },
            false,
        )
        .await
        .map(|result| result.terminal)
    }

    pub async fn recovery_inspect(
        &self,
        recovery_id: Uuid,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        self.call::<RecoveryResult, _>(
            "manual_sftp.recovery.inspect",
            &RecoveryParams { recovery_id },
            false,
        )
        .await
        .map(|result| result.recovery)
    }

    pub async fn recovery_execute(
        &self,
        recovery_id: Uuid,
        action: RecoveryAction,
        operation_id: Uuid,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        self.call::<RecoveryResult, _>(
            "manual_sftp.recovery.execute",
            &RecoveryExecuteParams {
                recovery_id,
                action,
                operation_id,
            },
            false,
        )
        .await
        .map(|result| result.recovery)
    }

    async fn entry_call(
        &self,
        method: &str,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteEntry, ManualSftpError> {
        self.call::<EntryResult, _>(
            method,
            &SessionPathParams {
                ssh_session_id,
                path,
            },
            false,
        )
        .await
        .map(|result| result.entry)
    }

    async fn terminal_call(
        &self,
        method: &str,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.call::<TerminalResult, _>(method, &OperationParams { operation_id }, false)
            .await
            .map(|result| result.terminal)
    }

    async fn call<T, P>(&self, method: &str, params: &P, secret: bool) -> Result<T, ManualSftpError>
    where
        T: DeserializeOwned,
        P: Serialize,
    {
        let params = serde_json::to_value(params)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(request_invalid)?;
        let payload = Map::from_iter([
            ("method".to_owned(), Value::String(method.to_owned())),
            ("params".to_owned(), Value::Object(params)),
        ]);
        let request = if secret {
            RuntimeRequest::secret(payload)
        } else {
            RuntimeRequest::normal(payload)
        };
        let frame = self.broker.request(request).await.map_err(|error| {
            ManualSftpError::uncertain_transport(
                error.error_code(),
                "The SFTP runtime is unavailable.",
            )
        })?;

        match frame.message_type {
            MessageType::Response => {
                serde_json::from_value(Value::Object(frame.payload)).map_err(|_| response_invalid())
            }
            MessageType::Error => {
                let code = frame
                    .payload
                    .get("error_code")
                    .and_then(Value::as_str)
                    .filter(|code| is_safe_error_code(code))
                    .unwrap_or("SIDECAR_REQUEST_FAILED");
                let retained_state = match frame.payload.get("operation_state") {
                    None | Some(Value::Null) => None,
                    Some(Value::String(value)) if value == "cleanup_required" => {
                        Some(RetainedOperationState::CleanupRequired)
                    }
                    Some(Value::String(value)) if value == "outcome_unknown" => {
                        Some(RetainedOperationState::OutcomeUnknown)
                    }
                    Some(_) => return Err(response_invalid()),
                };
                Err(ManualSftpError::trusted_remote_with_state(
                    code,
                    "The SFTP runtime rejected the request.",
                    retained_state,
                ))
            }
            _ => Err(response_invalid()),
        }
    }
}

fn response_invalid() -> ManualSftpError {
    ManualSftpError::uncertain_transport(
        "SIDECAR_RESPONSE_INVALID",
        "The SFTP runtime returned an invalid response.",
    )
}

fn request_invalid() -> ManualSftpError {
    ManualSftpError::new(
        "SIDECAR_REQUEST_INVALID",
        "The SFTP runtime request could not be encoded.",
    )
}

fn require_sequence(sequence: u32) -> Result<(), ManualSftpError> {
    if sequence > SFTP_SEQUENCE_MAX {
        return Err(ManualSftpError::new(
            "SFTP_SEQUENCE_INVALID",
            "The transfer sequence exceeds the supported range.",
        ));
    }
    Ok(())
}

fn is_safe_error_code(code: &str) -> bool {
    !code.is_empty()
        && code.len() <= 64
        && code
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct SessionParams {
    ssh_session_id: Uuid,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct SessionPathParams<'a> {
    ssh_session_id: Uuid,
    path: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct ListingNextParams {
    listing_id: Uuid,
    sequence: u32,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct ListingCloseParams {
    listing_id: Uuid,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct OperationParams {
    operation_id: Uuid,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct UploadBeginParams<'a> {
    operation_id: Uuid,
    ssh_session_id: Uuid,
    path: &'a str,
    source_sha256: &'a str,
    source_byte_count: u64,
    target_snapshot: &'a TransferSnapshot,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct UploadChunkParams<'a> {
    operation_id: Uuid,
    sequence: u32,
    offset: u64,
    chunk_b64: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct DownloadBeginParams<'a> {
    operation_id: Uuid,
    ssh_session_id: Uuid,
    path: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct DownloadChunkParams {
    operation_id: Uuid,
    sequence: u32,
    offset: u64,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct MkdirParams<'a> {
    operation_id: Uuid,
    ssh_session_id: Uuid,
    parent_path: &'a str,
    name: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct RenameParams<'a> {
    operation_id: Uuid,
    ssh_session_id: Uuid,
    source_path: &'a str,
    target_path: &'a str,
    overwrite: bool,
    source_snapshot: Option<&'a TransferSnapshot>,
    target_snapshot: Option<&'a TransferSnapshot>,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct RemoveParams<'a> {
    operation_id: Uuid,
    ssh_session_id: Uuid,
    path: &'a str,
    expected_snapshot: &'a TransferSnapshot,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct DeletePreflightParams<'a> {
    operation_id: Uuid,
    ssh_session_id: Uuid,
    path: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct DeleteExecuteParams {
    delete_plan_id: Uuid,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct RecoveryParams {
    recovery_id: Uuid,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct RecoveryExecuteParams {
    recovery_id: Uuid,
    action: RecoveryAction,
    operation_id: Uuid,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ContextResult {
    context: ManualSftpContext,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BatchResult {
    batch: ListingBatch,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ClosedResult {
    closed: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EntryResult {
    entry: RemoteEntry,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HashResult {
    hash: RemoteFileHash,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SnapshotResult {
    snapshot: TransferSnapshot,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UploadResult {
    upload: UploadReady,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UploadChunkResult {
    chunk: UploadChunkAck,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DownloadResult {
    download: DownloadReady,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DownloadChunkResult {
    chunk: DownloadChunk,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeletePlanResult {
    delete_plan: DeletePlanSummary,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TerminalResult {
    terminal: OperationTerminalProjection,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecoveryResult {
    recovery: RecoveryResponse,
}

/// Python recovery inspection/execution returns either an unresolved safe summary or a terminal
/// receipt. Inspection terminals identify the inspected remote operation; mutating execution
/// terminals identify Rust's fresh selected remote operation. Both payload shapes are decoded as
/// a strict union rather than guessed from optional fields.
#[derive(Debug, Deserialize, serde::Serialize)]
#[serde(untagged)]
pub enum RecoveryResponse {
    Summary(RecoverySummary),
    Terminal(OperationTerminalProjection),
}
