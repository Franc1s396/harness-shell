use bytes::Bytes;
use reqwest::{header::HeaderMap, Method, StatusCode};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::runtime::{
    http::private, models::CorrelatedResponse, RuntimeBinaryHttpRequest, RuntimeClient,
    RuntimeClientError, RuntimeClientHandle, RuntimeHttpRequest, RuntimeRequestBody,
};

use super::models::{
    DeletePlanSummary, DownloadChunk, DownloadReady, ListingBatch, ManualSftpContext,
    ManualSftpError, OperationTerminalProjection, RecoveryAction, RecoverySummary, RemoteEntry,
    RemoteFileHash, RetainedOperationState, TransferSnapshot, UploadChunkAck, UploadReady,
    JS_SAFE_INTEGER_MAX, SFTP_CHUNK_BYTES, SFTP_SEQUENCE_MAX,
};

/// Owns the sealed HTTP request mapping for user-operated Manual SFTP.
#[derive(Clone)]
pub struct ManualSftpRuntimeClient<R = RuntimeClientHandle> {
    runtime: R,
}

impl<R> ManualSftpRuntimeClient<R>
where
    R: RuntimeClient + Clone,
{
    pub fn new(runtime: R) -> Self {
        Self { runtime }
    }

    pub async fn open(&self, ssh_session_id: Uuid) -> Result<ManualSftpContext, ManualSftpError> {
        self.json(OpenContextRequest { ssh_session_id })
            .await
            .map(|response| response.context)
    }

    pub async fn list_begin(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<ListingBatch, ManualSftpError> {
        self.json(BeginListingRequest {
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.batch)
    }

    pub async fn list_next(
        &self,
        listing_id: Uuid,
        sequence: u32,
    ) -> Result<ListingBatch, ManualSftpError> {
        require_sequence(sequence)?;
        self.json(NextListingRequest {
            listing_id,
            sequence,
        })
        .await
        .map(|response| response.batch)
    }

    pub async fn list_close(&self, listing_id: Uuid) -> Result<bool, ManualSftpError> {
        self.json(CloseListingRequest { listing_id })
            .await
            .map(|response| response.closed)
    }

    pub async fn lstat(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteEntry, ManualSftpError> {
        self.json(LstatRequest {
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.entry)
    }

    pub async fn readlink(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteEntry, ManualSftpError> {
        self.json(ReadlinkRequest {
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.entry)
    }

    pub async fn realpath(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteEntry, ManualSftpError> {
        self.json(RealpathRequest {
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.entry)
    }

    pub async fn sha256(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<RemoteFileHash, ManualSftpError> {
        self.json(Sha256Request {
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.hash)
    }

    pub async fn upload_preflight(
        &self,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<TransferSnapshot, ManualSftpError> {
        self.json(UploadPreflightRequest {
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.snapshot)
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
        self.json(BeginUploadRequest {
            operation_id,
            ssh_session_id,
            path,
            source_sha256,
            source_byte_count,
            target_snapshot,
        })
        .await
        .map(|response| response.upload)
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
        if chunk.is_empty() || chunk.len() > SFTP_CHUNK_BYTES {
            return Err(ManualSftpError::new(
                "SFTP_CHUNK_LIMIT_EXCEEDED",
                "The transfer chunk is empty or exceeds the supported size.",
            ));
        }
        let response = self
            .json(UploadChunkRequest {
                operation_id,
                sequence,
                offset,
                bytes: chunk.to_vec(),
            })
            .await?;
        if response.operation_id != operation_id
            || response.sequence != sequence
            || response.offset != offset
            || response.accepted_bytes != chunk.len()
        {
            return Err(response_invalid());
        }
        let next_offset = offset
            .checked_add(response.accepted_bytes as u64)
            .filter(|value| *value <= JS_SAFE_INTEGER_MAX)
            .ok_or_else(response_invalid)?;
        let next_sequence = sequence.checked_add(1).ok_or_else(response_invalid)?;
        Ok(UploadChunkAck {
            operation_id,
            next_sequence,
            next_offset,
        })
    }

    pub async fn upload_finish(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.json(FinishUploadRequest { operation_id })
            .await
            .map(|response| response.terminal)
    }

    pub async fn upload_abort(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.json(AbortUploadRequest { operation_id })
            .await
            .map(|response| response.terminal)
    }

    pub async fn download_begin(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<DownloadReady, ManualSftpError> {
        self.json(BeginDownloadRequest {
            operation_id,
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.download)
    }

    pub async fn download_chunk(
        &self,
        operation_id: Uuid,
        sequence: u32,
        offset: u64,
    ) -> Result<DownloadChunk, ManualSftpError> {
        require_sequence(sequence)?;
        super::models::require_js_safe(offset)?;
        self.runtime
            .execute_binary(DownloadChunkRequest {
                operation_id,
                sequence,
                offset,
            })
            .await
            .map_err(map_sftp_runtime_error)
    }

    pub async fn download_finish(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.json(FinishDownloadRequest { operation_id })
            .await
            .map(|response| response.terminal)
    }

    pub async fn download_abort(
        &self,
        operation_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.json(AbortDownloadRequest { operation_id })
            .await
            .map(|response| response.terminal)
    }

    pub async fn mkdir(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        parent_path: &str,
        name: &str,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.json(MkdirRequest {
            operation_id,
            ssh_session_id,
            parent_path,
            name,
        })
        .await
        .map(|response| response.terminal)
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
        self.json(RenameRequest {
            operation_id,
            ssh_session_id,
            source_path,
            target_path,
            overwrite,
            source_snapshot,
            target_snapshot,
        })
        .await
        .map(|response| response.terminal)
    }

    pub async fn remove(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: &str,
        expected_snapshot: &TransferSnapshot,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.json(RemoveRequest {
            operation_id,
            ssh_session_id,
            path,
            expected_snapshot,
        })
        .await
        .map(|response| response.terminal)
    }

    pub async fn delete_preflight(
        &self,
        operation_id: Uuid,
        ssh_session_id: Uuid,
        path: &str,
    ) -> Result<DeletePlanSummary, ManualSftpError> {
        self.json(DeletePreflightRequest {
            operation_id,
            ssh_session_id,
            path,
        })
        .await
        .map(|response| response.delete_plan)
    }

    pub async fn delete_execute(
        &self,
        delete_plan_id: Uuid,
    ) -> Result<OperationTerminalProjection, ManualSftpError> {
        self.json(DeleteExecuteRequest {
            operation_id: delete_plan_id,
        })
        .await
        .map(|response| response.terminal)
    }

    pub async fn recovery_inspect(
        &self,
        recovery_id: Uuid,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        self.json(InspectRecoveryRequest { recovery_id })
            .await
            .map(|response| response.recovery)
    }

    pub async fn recovery_execute(
        &self,
        recovery_id: Uuid,
        action: RecoveryAction,
        operation_id: Uuid,
    ) -> Result<RecoveryResponse, ManualSftpError> {
        self.json(ExecuteRecoveryRequest {
            recovery_id,
            action,
            operation_id,
        })
        .await
        .map(|response| response.recovery)
    }

    async fn json<Q>(&self, request: Q) -> Result<Q::Response, ManualSftpError>
    where
        Q: RuntimeHttpRequest,
    {
        self.runtime
            .execute(request)
            .await
            .map_err(map_sftp_runtime_error)
    }
}

fn map_sftp_runtime_error(error: RuntimeClientError) -> ManualSftpError {
    let Some(problem) = error.problem() else {
        return ManualSftpError::uncertain_transport(
            error.error_code(),
            "The SFTP runtime is unavailable or returned an invalid response.",
        );
    };
    if !is_safe_error_code(&problem.error_code) {
        return response_invalid();
    }
    let retained_state = match problem.details.get("operation_state") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::String(value)) if value == "cleanup_required" => {
            Some(RetainedOperationState::CleanupRequired)
        }
        Some(serde_json::Value::String(value)) if value == "outcome_unknown" => {
            Some(RetainedOperationState::OutcomeUnknown)
        }
        Some(_) => return response_invalid(),
    };
    ManualSftpError::trusted_remote_with_state(
        problem.error_code.clone(),
        "The SFTP runtime rejected the request.",
        retained_state,
    )
}

fn response_invalid() -> ManualSftpError {
    ManualSftpError::uncertain_transport(
        "SIDECAR_RESPONSE_INVALID",
        "The SFTP runtime returned an invalid response.",
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

macro_rules! response {
    ($name:ident, $field:ident : $ty:ty) => {
        #[derive(Debug, Deserialize)]
        #[serde(deny_unknown_fields)]
        pub struct $name {
            pub request_id: Uuid,
            pub $field: $ty,
        }

        impl CorrelatedResponse for $name {
            fn request_id(&self) -> Uuid {
                self.request_id
            }
        }
    };
}

response!(ContextResponse, context: ManualSftpContext);
response!(ListingResponse, batch: ListingBatch);
response!(ClosedResponse, closed: bool);
response!(EntryResponse, entry: RemoteEntry);
response!(HashResponse, hash: RemoteFileHash);
response!(SnapshotResponse, snapshot: TransferSnapshot);
response!(UploadResponse, upload: UploadReady);
response!(DownloadResponse, download: DownloadReady);
response!(TerminalResponse, terminal: OperationTerminalProjection);
response!(DeletePlanResponse, delete_plan: DeletePlanSummary);
response!(RecoveryResult, recovery: RecoveryResponse);

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UploadChunkResponse {
    pub request_id: Uuid,
    pub operation_id: Uuid,
    pub sequence: u32,
    pub offset: u64,
    pub accepted_bytes: usize,
}

impl CorrelatedResponse for UploadChunkResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

macro_rules! json_request {
    ($name:ident, $response:ty, $method:expr, $status:expr, $path:expr, { $($field:ident : $ty:ty),* $(,)? }) => {
        #[derive(Debug, Serialize)]
        pub struct $name { $(pub $field: $ty,)* }
        impl private::Sealed for $name {}
        impl RuntimeHttpRequest for $name {
            type Response = $response;
            fn method(&self) -> Method { $method }
            fn path(&self) -> String { ($path)(self) }
            fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> { json_body(self) }
            fn success_status(&self) -> StatusCode { $status }
        }
    };
    ($name:ident<$l:lifetime>, $response:ty, $method:expr, $status:expr, $path:expr, { $($field:ident : $ty:ty),* $(,)? }) => {
        #[derive(Debug, Serialize)]
        pub struct $name<$l> { $(pub $field: $ty,)* }
        impl<$l> private::Sealed for $name<$l> {}
        impl<$l> RuntimeHttpRequest for $name<$l> {
            type Response = $response;
            fn method(&self) -> Method { $method }
            fn path(&self) -> String { ($path)(self) }
            fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> { json_body(self) }
            fn success_status(&self) -> StatusCode { $status }
        }
    };
}

macro_rules! empty_request {
    ($name:ident, $response:ty, $method:expr, $status:expr, $path:expr, { $($field:ident : $ty:ty),* $(,)? }) => {
        #[derive(Clone, Copy, Debug)]
        pub struct $name { $(pub $field: $ty,)* }
        impl private::Sealed for $name {}
        impl RuntimeHttpRequest for $name {
            type Response = $response;
            fn method(&self) -> Method { $method }
            fn path(&self) -> String { ($path)(self) }
            fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> { Ok(RuntimeRequestBody::Empty) }
            fn success_status(&self) -> StatusCode { $status }
        }
    };
}

fn json_body(value: &impl Serialize) -> Result<RuntimeRequestBody, RuntimeClientError> {
    serde_json::to_vec(value)
        .map(RuntimeRequestBody::Json)
        .map_err(|_| RuntimeClientError::HttpContract {
            reason: "request serialization failed",
        })
}

json_request!(OpenContextRequest, ContextResponse, Method::POST, StatusCode::CREATED, |_: &OpenContextRequest| "/v1/sftp/contexts".into(), { ssh_session_id: Uuid });
json_request!(BeginListingRequest<'a>, ListingResponse, Method::POST, StatusCode::CREATED, |_: &BeginListingRequest<'_>| "/v1/sftp/listings".into(), { ssh_session_id: Uuid, path: &'a str });
empty_request!(NextListingRequest, ListingResponse, Method::GET, StatusCode::OK, |r: &NextListingRequest| format!("/v1/sftp/listings/{}/batches/{}", r.listing_id, r.sequence), { listing_id: Uuid, sequence: u32 });
empty_request!(CloseListingRequest, ClosedResponse, Method::DELETE, StatusCode::OK, |r: &CloseListingRequest| format!("/v1/sftp/listings/{}", r.listing_id), { listing_id: Uuid });
json_request!(LstatRequest<'a>, EntryResponse, Method::POST, StatusCode::OK, |_: &LstatRequest<'_>| "/v1/sftp/metadata/lstat".into(), { ssh_session_id: Uuid, path: &'a str });
json_request!(ReadlinkRequest<'a>, EntryResponse, Method::POST, StatusCode::OK, |_: &ReadlinkRequest<'_>| "/v1/sftp/metadata/readlink".into(), { ssh_session_id: Uuid, path: &'a str });
json_request!(RealpathRequest<'a>, EntryResponse, Method::POST, StatusCode::OK, |_: &RealpathRequest<'_>| "/v1/sftp/metadata/realpath".into(), { ssh_session_id: Uuid, path: &'a str });
json_request!(Sha256Request<'a>, HashResponse, Method::POST, StatusCode::OK, |_: &Sha256Request<'_>| "/v1/sftp/hashes/sha256".into(), { ssh_session_id: Uuid, path: &'a str });
json_request!(UploadPreflightRequest<'a>, SnapshotResponse, Method::POST, StatusCode::OK, |_: &UploadPreflightRequest<'_>| "/v1/sftp/uploads/preflight".into(), { ssh_session_id: Uuid, path: &'a str });
json_request!(BeginUploadRequest<'a>, UploadResponse, Method::POST, StatusCode::CREATED, |_: &BeginUploadRequest<'_>| "/v1/sftp/uploads".into(), { operation_id: Uuid, ssh_session_id: Uuid, path: &'a str, source_sha256: &'a str, source_byte_count: u64, target_snapshot: &'a TransferSnapshot });

#[derive(Debug)]
pub struct UploadChunkRequest {
    pub operation_id: Uuid,
    pub sequence: u32,
    pub offset: u64,
    pub bytes: Vec<u8>,
}
impl private::Sealed for UploadChunkRequest {}
impl RuntimeHttpRequest for UploadChunkRequest {
    type Response = UploadChunkResponse;
    fn method(&self) -> Method {
        Method::PUT
    }
    fn path(&self) -> String {
        format!(
            "/v1/sftp/uploads/{}/chunks/{}",
            self.operation_id, self.sequence
        )
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Binary {
            bytes: self.bytes.clone(),
            offset: self.offset,
        })
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

empty_request!(FinishUploadRequest, TerminalResponse, Method::POST, StatusCode::OK, |r: &FinishUploadRequest| format!("/v1/sftp/uploads/{}/finish", r.operation_id), { operation_id: Uuid });
empty_request!(AbortUploadRequest, TerminalResponse, Method::POST, StatusCode::OK, |r: &AbortUploadRequest| format!("/v1/sftp/uploads/{}/abort", r.operation_id), { operation_id: Uuid });
json_request!(BeginDownloadRequest<'a>, DownloadResponse, Method::POST, StatusCode::CREATED, |_: &BeginDownloadRequest<'_>| "/v1/sftp/downloads".into(), { operation_id: Uuid, ssh_session_id: Uuid, path: &'a str });

#[derive(Clone, Copy, Debug)]
pub struct DownloadChunkRequest {
    pub operation_id: Uuid,
    pub sequence: u32,
    pub offset: u64,
}
impl private::Sealed for DownloadChunkRequest {}
impl RuntimeBinaryHttpRequest for DownloadChunkRequest {
    type Response = DownloadChunk;
    fn method(&self) -> Method {
        Method::GET
    }
    fn path(&self) -> String {
        format!(
            "/v1/sftp/downloads/{}/chunks/{}?offset={}",
            self.operation_id, self.sequence, self.offset
        )
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Empty)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
    fn maximum_response_bytes(&self) -> usize {
        SFTP_CHUNK_BYTES
    }
    fn decode_success(
        &self,
        _request_id: Uuid,
        headers: &HeaderMap,
        body: Bytes,
    ) -> Result<Self::Response, RuntimeClientError> {
        let sequence = canonical_header_u64(headers, "X-Chunk-Sequence")?;
        let offset = canonical_header_u64(headers, "X-Chunk-Offset")?;
        let byte_count = canonical_header_u64(headers, "X-Chunk-Byte-Count")?;
        let eof = match unique_header_text(headers, "X-Chunk-EOF")? {
            "true" => true,
            "false" => false,
            _ => return Err(contract("invalid chunk EOF header")),
        };
        if sequence != self.sequence as u64
            || offset != self.offset
            || byte_count != body.len() as u64
            || body.len() > SFTP_CHUNK_BYTES
            || (body.is_empty() && !eof)
        {
            return Err(contract("download chunk identity mismatch"));
        }
        let next_offset = offset
            .checked_add(byte_count)
            .filter(|value| *value <= JS_SAFE_INTEGER_MAX)
            .ok_or_else(|| contract("download chunk offset overflow"))?;
        Ok(DownloadChunk {
            operation_id: self.operation_id,
            sequence: self.sequence,
            offset,
            bytes: body,
            next_offset,
            eof,
        })
    }
}

empty_request!(FinishDownloadRequest, TerminalResponse, Method::POST, StatusCode::OK, |r: &FinishDownloadRequest| format!("/v1/sftp/downloads/{}/finish", r.operation_id), { operation_id: Uuid });
empty_request!(AbortDownloadRequest, TerminalResponse, Method::POST, StatusCode::OK, |r: &AbortDownloadRequest| format!("/v1/sftp/downloads/{}/abort", r.operation_id), { operation_id: Uuid });
json_request!(MkdirRequest<'a>, TerminalResponse, Method::POST, StatusCode::CREATED, |_: &MkdirRequest<'_>| "/v1/sftp/directories".into(), { operation_id: Uuid, ssh_session_id: Uuid, parent_path: &'a str, name: &'a str });
json_request!(RenameRequest<'a>, TerminalResponse, Method::POST, StatusCode::OK, |_: &RenameRequest<'_>| "/v1/sftp/renames".into(), { operation_id: Uuid, ssh_session_id: Uuid, source_path: &'a str, target_path: &'a str, overwrite: bool, source_snapshot: Option<&'a TransferSnapshot>, target_snapshot: Option<&'a TransferSnapshot> });
json_request!(RemoveRequest<'a>, TerminalResponse, Method::POST, StatusCode::OK, |_: &RemoveRequest<'_>| "/v1/sftp/removals".into(), { operation_id: Uuid, ssh_session_id: Uuid, path: &'a str, expected_snapshot: &'a TransferSnapshot });
json_request!(DeletePreflightRequest<'a>, DeletePlanResponse, Method::POST, StatusCode::OK, |_: &DeletePreflightRequest<'_>| "/v1/sftp/deletions/preflight".into(), { operation_id: Uuid, ssh_session_id: Uuid, path: &'a str });
empty_request!(DeleteExecuteRequest, TerminalResponse, Method::POST, StatusCode::OK, |r: &DeleteExecuteRequest| format!("/v1/sftp/deletions/{}/execute", r.operation_id), { operation_id: Uuid });
empty_request!(InspectRecoveryRequest, RecoveryResult, Method::GET, StatusCode::OK, |r: &InspectRecoveryRequest| format!("/v1/sftp/recoveries?recovery_id={}", r.recovery_id), { recovery_id: Uuid });

#[derive(Debug, Serialize)]
pub struct ExecuteRecoveryRequest {
    #[serde(skip)]
    pub recovery_id: Uuid,
    pub action: RecoveryAction,
    pub operation_id: Uuid,
}
impl private::Sealed for ExecuteRecoveryRequest {}
impl RuntimeHttpRequest for ExecuteRecoveryRequest {
    type Response = RecoveryResult;
    fn method(&self) -> Method {
        Method::POST
    }
    fn path(&self) -> String {
        format!("/v1/sftp/recoveries/{}/actions", self.recovery_id)
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        json_body(self)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

fn unique_header_text<'a>(
    headers: &'a HeaderMap,
    name: &str,
) -> Result<&'a str, RuntimeClientError> {
    let mut values = headers.get_all(name).iter();
    let value = values
        .next()
        .ok_or_else(|| contract("required chunk header missing"))?;
    if values.next().is_some() {
        return Err(contract("duplicate chunk header"));
    }
    value
        .to_str()
        .map_err(|_| contract("invalid chunk header text"))
}

fn canonical_header_u64(headers: &HeaderMap, name: &str) -> Result<u64, RuntimeClientError> {
    let value = unique_header_text(headers, name)?;
    if value.is_empty()
        || (value.len() > 1 && value.starts_with('0'))
        || !value.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(contract("non-canonical chunk header integer"));
    }
    value
        .parse()
        .map_err(|_| contract("chunk header integer overflow"))
}

fn contract(reason: &'static str) -> RuntimeClientError {
    RuntimeClientError::HttpContract { reason }
}

/// Recovery routes return either an unresolved safe summary or a terminal receipt.
#[derive(Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum RecoveryResponse {
    Summary(RecoverySummary),
    Terminal(OperationTerminalProjection),
}
