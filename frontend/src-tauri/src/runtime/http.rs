use std::fmt;

use reqwest::{
    header::{HeaderMap, HeaderValue, ACCEPT, CONTENT_TYPE},
    Method, StatusCode,
};
use serde::{de::DeserializeOwned, Serialize};
use uuid::Uuid;
use zeroize::Zeroize;

use crate::{
    commands::{ConnectionProfileInput, HostKeyCandidate, ModelApiConfigInput},
    vault::CredentialId,
};

use super::client::RuntimeClientError;
use super::models::{
    AgentApiConfigListResponse, AgentApiConfigResponse, AgentTurnResponse, ConnectionListResponse,
    ConnectionResponse, CorrelatedResponse, DeleteResponse, HealthLiveResponse,
    HealthReadyResponse, HostKeyResponse, ProblemDetails, PtySessionResponse,
    RequestCancelResponse, RuntimeInitializeBody, RuntimeStateResponse, SshStatusResponse,
    JSON_BODY_MAX_BYTES,
};

const REQUEST_ID_HEADER: &str = "X-Request-ID";

pub(crate) mod private {
    pub trait Sealed {}
}

pub trait RuntimeHttpRequest: private::Sealed + Send + Sync {
    type Response: DeserializeOwned + CorrelatedResponse;

    fn method(&self) -> Method;
    fn path(&self) -> String;
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError>;
    fn success_status(&self) -> StatusCode;
}

/// Sealed request contract for raw binary success responses. Problem Details
/// failures still use the same strict JSON correlation checks as normal HTTP.
pub trait RuntimeBinaryHttpRequest: private::Sealed + Send + Sync {
    type Response;

    fn method(&self) -> Method;
    fn path(&self) -> String;
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError>;
    fn success_status(&self) -> StatusCode;
    fn maximum_response_bytes(&self) -> usize;
    fn decode_success(
        &self,
        request_id: Uuid,
        headers: &HeaderMap,
        body: bytes::Bytes,
    ) -> Result<Self::Response, RuntimeClientError>;
}

pub enum RuntimeRequestBody {
    Empty,
    Json(Vec<u8>),
    Binary { bytes: Vec<u8>, offset: u64 },
}

impl fmt::Debug for RuntimeRequestBody {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => formatter.write_str("Empty"),
            Self::Json(bytes) => formatter
                .debug_struct("Json")
                .field("bytes", &bytes.len())
                .finish(),
            Self::Binary { bytes, offset } => formatter
                .debug_struct("Binary")
                .field("bytes", &bytes.len())
                .field("offset", offset)
                .finish(),
        }
    }
}

impl Drop for RuntimeRequestBody {
    fn drop(&mut self) {
        match self {
            Self::Empty => {}
            Self::Json(bytes) | Self::Binary { bytes, .. } => bytes.zeroize(),
        }
    }
}

#[derive(Clone)]
pub struct TypedHttpClient {
    client: reqwest::Client,
    base_url: url::Url,
}

impl TypedHttpClient {
    pub fn new(port: u16) -> Result<Self, RuntimeClientError> {
        if port == 0 {
            return Err(RuntimeClientError::Configuration);
        }
        let base_url = url::Url::parse(&format!("http://127.0.0.1:{port}/"))
            .map_err(|_| RuntimeClientError::Configuration)?;
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| RuntimeClientError::Configuration)?;
        Ok(Self { client, base_url })
    }

    pub async fn execute<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeHttpRequest,
    {
        let request_id = Uuid::new_v4();
        let url = self
            .base_url
            .join(request.path().trim_start_matches('/'))
            .map_err(|_| RuntimeClientError::Configuration)?;
        if url.host_str() != Some("127.0.0.1") || url.port() != self.base_url.port() {
            return Err(RuntimeClientError::Configuration);
        }
        let mut builder = self
            .client
            .request(request.method(), url)
            .header(REQUEST_ID_HEADER, request_id.to_string())
            .header(ACCEPT, "application/json, application/problem+json");
        let body = request.body()?;
        match &body {
            RuntimeRequestBody::Empty => {}
            RuntimeRequestBody::Json(bytes) => {
                if bytes.len() > JSON_BODY_MAX_BYTES {
                    return Err(RuntimeClientError::HttpRequestTooLarge);
                }
                builder = builder
                    .header(CONTENT_TYPE, "application/json")
                    .body(bytes.clone());
            }
            RuntimeRequestBody::Binary { bytes, offset } => {
                builder = builder
                    .header(CONTENT_TYPE, "application/octet-stream")
                    .header("X-Chunk-Offset", offset.to_string())
                    .body(bytes.clone());
            }
        }
        let response = builder
            .send()
            .await
            .map_err(|_| RuntimeClientError::HttpTransport)?;
        let status = response.status();
        let headers = response.headers().clone();
        let header_request_id = unique_request_id(&headers)?;
        if header_request_id != request_id {
            return Err(RuntimeClientError::HttpContract {
                reason: "response request ID mismatch",
            });
        }
        let content_type = unique_content_type(&headers)?;
        let body = read_bounded(response, JSON_BODY_MAX_BYTES).await?;
        if status.is_success() {
            if status != request.success_status() || content_type != "application/json" {
                return Err(RuntimeClientError::HttpContract {
                    reason: "success status or content type mismatch",
                });
            }
            let decoded: R::Response =
                serde_json::from_slice(&body).map_err(|_| RuntimeClientError::HttpContract {
                    reason: "invalid success response body",
                })?;
            if decoded.request_id() != request_id {
                return Err(RuntimeClientError::HttpContract {
                    reason: "success body request ID mismatch",
                });
            }
            return Ok(decoded);
        }
        if content_type != "application/problem+json" {
            return Err(RuntimeClientError::HttpContract {
                reason: "problem content type mismatch",
            });
        }
        let problem: ProblemDetails =
            serde_json::from_slice(&body).map_err(|_| RuntimeClientError::HttpContract {
                reason: "invalid Problem Details body",
            })?;
        if problem.request_id != request_id || problem.status != status.as_u16() {
            return Err(RuntimeClientError::HttpContract {
                reason: "Problem Details correlation mismatch",
            });
        }
        Err(RuntimeClientError::Problem(problem))
    }

    pub async fn execute_binary<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeBinaryHttpRequest,
    {
        let request_id = Uuid::new_v4();
        let url = self
            .base_url
            .join(request.path().trim_start_matches('/'))
            .map_err(|_| RuntimeClientError::Configuration)?;
        if url.host_str() != Some("127.0.0.1") || url.port() != self.base_url.port() {
            return Err(RuntimeClientError::Configuration);
        }
        let mut builder = self
            .client
            .request(request.method(), url)
            .header(REQUEST_ID_HEADER, request_id.to_string())
            .header(ACCEPT, "application/octet-stream, application/problem+json");
        let request_body = request.body()?;
        match &request_body {
            RuntimeRequestBody::Empty => {}
            RuntimeRequestBody::Json(_) => {
                return Err(RuntimeClientError::HttpContract {
                    reason: "binary request cannot use a JSON body",
                });
            }
            RuntimeRequestBody::Binary { bytes, offset } => {
                builder = builder
                    .header(CONTENT_TYPE, "application/octet-stream")
                    .header("X-Chunk-Offset", offset.to_string())
                    .body(bytes.clone());
            }
        }
        let response = builder
            .send()
            .await
            .map_err(|_| RuntimeClientError::HttpTransport)?;
        let status = response.status();
        let headers = response.headers().clone();
        let header_request_id = unique_request_id(&headers)?;
        if header_request_id != request_id {
            return Err(RuntimeClientError::HttpContract {
                reason: "response request ID mismatch",
            });
        }
        let content_type = unique_content_type(&headers)?;
        let maximum = if status.is_success() {
            request.maximum_response_bytes()
        } else {
            JSON_BODY_MAX_BYTES
        };
        let body = bytes::Bytes::from(read_bounded(response, maximum).await?);
        if status.is_success() {
            if status != request.success_status() || content_type != "application/octet-stream" {
                return Err(RuntimeClientError::HttpContract {
                    reason: "binary success status or content type mismatch",
                });
            }
            return request.decode_success(request_id, &headers, body);
        }
        if content_type != "application/problem+json" {
            return Err(RuntimeClientError::HttpContract {
                reason: "problem content type mismatch",
            });
        }
        let problem: ProblemDetails =
            serde_json::from_slice(&body).map_err(|_| RuntimeClientError::HttpContract {
                reason: "invalid Problem Details body",
            })?;
        if problem.request_id != request_id || problem.status != status.as_u16() {
            return Err(RuntimeClientError::HttpContract {
                reason: "Problem Details correlation mismatch",
            });
        }
        Err(RuntimeClientError::Problem(problem))
    }
}

async fn read_bounded(
    mut response: reqwest::Response,
    maximum: usize,
) -> Result<Vec<u8>, RuntimeClientError> {
    if response
        .content_length()
        .is_some_and(|length| length > maximum as u64)
    {
        return Err(RuntimeClientError::HttpResponseTooLarge);
    }
    let mut body = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| RuntimeClientError::HttpTransport)?
    {
        if body.len().saturating_add(chunk.len()) > maximum {
            return Err(RuntimeClientError::HttpResponseTooLarge);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn unique_header<'a>(
    headers: &'a HeaderMap,
    name: &str,
) -> Result<&'a HeaderValue, RuntimeClientError> {
    let mut values = headers.get_all(name).iter();
    let value = values.next().ok_or(RuntimeClientError::HttpContract {
        reason: "required response header missing",
    })?;
    if values.next().is_some() {
        return Err(RuntimeClientError::HttpContract {
            reason: "duplicate response header",
        });
    }
    Ok(value)
}

fn unique_request_id(headers: &HeaderMap) -> Result<Uuid, RuntimeClientError> {
    unique_header(headers, REQUEST_ID_HEADER)?
        .to_str()
        .ok()
        .and_then(|value| Uuid::parse_str(value).ok())
        .ok_or(RuntimeClientError::HttpContract {
            reason: "invalid response request ID",
        })
}

fn unique_content_type(headers: &HeaderMap) -> Result<&str, RuntimeClientError> {
    unique_header(headers, CONTENT_TYPE.as_str())?
        .to_str()
        .map_err(|_| RuntimeClientError::HttpContract {
            reason: "invalid response content type",
        })
}

#[derive(Clone, Copy, Debug, Default)]
pub struct HealthLiveRequest;

impl private::Sealed for HealthLiveRequest {}

impl RuntimeHttpRequest for HealthLiveRequest {
    type Response = HealthLiveResponse;

    fn method(&self) -> Method {
        Method::GET
    }
    fn path(&self) -> String {
        "/v1/health/live".into()
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Empty)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct HealthReadyRequest;

impl private::Sealed for HealthReadyRequest {}

impl RuntimeHttpRequest for HealthReadyRequest {
    type Response = HealthReadyResponse;

    fn method(&self) -> Method {
        Method::GET
    }
    fn path(&self) -> String {
        "/v1/health/ready".into()
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Empty)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct ListConnectionsRequest;

impl private::Sealed for ListConnectionsRequest {}

impl RuntimeHttpRequest for ListConnectionsRequest {
    type Response = ConnectionListResponse;

    fn method(&self) -> Method {
        Method::GET
    }
    fn path(&self) -> String {
        "/v1/connections".into()
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Empty)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

#[derive(Debug)]
pub struct InitializeRuntimeRequest(pub RuntimeInitializeBody);

impl private::Sealed for InitializeRuntimeRequest {}

impl RuntimeHttpRequest for InitializeRuntimeRequest {
    type Response = RuntimeStateResponse;

    fn method(&self) -> Method {
        Method::POST
    }
    fn path(&self) -> String {
        "/v1/runtime/initialize".into()
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        json_body(&self.0)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct RuntimeStateRequest;

impl private::Sealed for RuntimeStateRequest {}

impl RuntimeHttpRequest for RuntimeStateRequest {
    type Response = RuntimeStateResponse;

    fn method(&self) -> Method {
        Method::GET
    }
    fn path(&self) -> String {
        "/v1/runtime/state".into()
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Empty)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct ShutdownRuntimeRequest;

impl private::Sealed for ShutdownRuntimeRequest {}

impl RuntimeHttpRequest for ShutdownRuntimeRequest {
    type Response = RuntimeStateResponse;

    fn method(&self) -> Method {
        Method::POST
    }
    fn path(&self) -> String {
        "/v1/runtime/shutdown".into()
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Empty)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::ACCEPTED
    }
}

#[derive(Clone, Copy, Debug)]
pub struct CancelRequest {
    pub target_request_id: Uuid,
}

impl private::Sealed for CancelRequest {}

impl RuntimeHttpRequest for CancelRequest {
    type Response = RequestCancelResponse;

    fn method(&self) -> Method {
        Method::POST
    }
    fn path(&self) -> String {
        format!("/v1/requests/{}/cancel", self.target_request_id)
    }
    fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
        Ok(RuntimeRequestBody::Empty)
    }
    fn success_status(&self) -> StatusCode {
        StatusCode::OK
    }
}

macro_rules! impl_json_request {
    ($request:ty, $response:ty, $method:expr, $path:expr, $status:expr) => {
        impl private::Sealed for $request {}

        impl RuntimeHttpRequest for $request {
            type Response = $response;

            fn method(&self) -> Method {
                $method
            }
            fn path(&self) -> String {
                ($path)(self)
            }
            fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
                json_body(self)
            }
            fn success_status(&self) -> StatusCode {
                $status
            }
        }
    };
}

macro_rules! impl_empty_request {
    ($request:ty, $response:ty, $method:expr, $path:expr, $status:expr) => {
        impl private::Sealed for $request {}

        impl RuntimeHttpRequest for $request {
            type Response = $response;

            fn method(&self) -> Method {
                $method
            }
            fn path(&self) -> String {
                ($path)(self)
            }
            fn body(&self) -> Result<RuntimeRequestBody, RuntimeClientError> {
                Ok(RuntimeRequestBody::Empty)
            }
            fn success_status(&self) -> StatusCode {
                $status
            }
        }
    };
}

#[derive(Debug, Serialize)]
pub struct CreateConnectionRequest(pub ConnectionProfileInput);

impl_json_request!(
    CreateConnectionRequest,
    ConnectionResponse,
    Method::POST,
    |_: &CreateConnectionRequest| "/v1/connections".into(),
    StatusCode::CREATED
);

#[derive(Clone, Copy, Debug)]
pub struct GetConnectionRequest {
    pub connection_id: Uuid,
}

impl_empty_request!(
    GetConnectionRequest,
    ConnectionResponse,
    Method::GET,
    |request: &GetConnectionRequest| format!("/v1/connections/{}", request.connection_id),
    StatusCode::OK
);

#[derive(Debug, Serialize)]
pub struct UpdateConnectionRequest {
    #[serde(skip)]
    pub connection_id: Uuid,
    #[serde(flatten)]
    pub input: ConnectionProfileInput,
}

impl_json_request!(
    UpdateConnectionRequest,
    ConnectionResponse,
    Method::PATCH,
    |request: &UpdateConnectionRequest| format!("/v1/connections/{}", request.connection_id),
    StatusCode::OK
);

#[derive(Clone, Copy, Debug)]
pub struct DeleteConnectionRequest {
    pub connection_id: Uuid,
}

impl_empty_request!(
    DeleteConnectionRequest,
    DeleteResponse,
    Method::DELETE,
    |request: &DeleteConnectionRequest| format!("/v1/connections/{}", request.connection_id),
    StatusCode::OK
);

#[derive(Debug, Serialize)]
pub struct ConfirmHostKeyRequest(pub HostKeyCandidate);

impl_json_request!(
    ConfirmHostKeyRequest,
    HostKeyResponse,
    Method::POST,
    |_: &ConfirmHostKeyRequest| "/v1/host-key-confirmations".into(),
    StatusCode::CREATED
);

#[derive(Debug, Serialize)]
pub struct ReplaceHostKeyRequest {
    #[serde(flatten)]
    pub candidate: HostKeyCandidate,
    pub expected_old_fingerprint: String,
}

impl_json_request!(
    ReplaceHostKeyRequest,
    HostKeyResponse,
    Method::POST,
    |_: &ReplaceHostKeyRequest| "/v1/host-key-replacements".into(),
    StatusCode::OK
);

#[derive(Serialize)]
pub struct SshJumpRequest {
    pub connection_id: Uuid,
    pub profile_version: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub password_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub private_key_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub passphrase_b64: Option<String>,
}

impl SshJumpRequest {
    pub fn password(connection_id: Uuid, profile_version: u64, password_b64: String) -> Self {
        Self {
            connection_id,
            profile_version,
            password_b64: Some(password_b64),
            private_key_b64: None,
            passphrase_b64: None,
        }
    }

    pub fn private_key(
        connection_id: Uuid,
        profile_version: u64,
        private_key_b64: String,
        passphrase_b64: Option<String>,
    ) -> Self {
        Self {
            connection_id,
            profile_version,
            password_b64: None,
            private_key_b64: Some(private_key_b64),
            passphrase_b64,
        }
    }
}

impl fmt::Debug for SshJumpRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SshJumpRequest")
            .field("connection_id", &self.connection_id)
            .field("profile_version", &self.profile_version)
            .field("authentication", &"<redacted>")
            .finish()
    }
}

impl Drop for SshJumpRequest {
    fn drop(&mut self) {
        zeroize_optional(&mut self.password_b64);
        zeroize_optional(&mut self.private_key_b64);
        zeroize_optional(&mut self.passphrase_b64);
    }
}

#[derive(Debug, Serialize)]
pub struct InspectHostKeyRequest {
    pub connection_id: Uuid,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub jump: Option<SshJumpRequest>,
}

impl_json_request!(
    InspectHostKeyRequest,
    SshStatusResponse,
    Method::POST,
    |_: &InspectHostKeyRequest| "/v1/host-key-inspections".into(),
    StatusCode::OK
);

#[derive(Serialize)]
pub struct ConnectSshRequest {
    pub connection_id: Uuid,
    pub profile_version: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub password_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub private_key_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub passphrase_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub jump: Option<SshJumpRequest>,
}

impl ConnectSshRequest {
    pub fn password(
        connection_id: Uuid,
        profile_version: u64,
        password_b64: String,
        jump: Option<SshJumpRequest>,
    ) -> Self {
        Self {
            connection_id,
            profile_version,
            password_b64: Some(password_b64),
            private_key_b64: None,
            passphrase_b64: None,
            jump,
        }
    }

    pub fn private_key(
        connection_id: Uuid,
        profile_version: u64,
        private_key_b64: String,
        passphrase_b64: Option<String>,
        jump: Option<SshJumpRequest>,
    ) -> Self {
        Self {
            connection_id,
            profile_version,
            password_b64: None,
            private_key_b64: Some(private_key_b64),
            passphrase_b64,
            jump,
        }
    }
}

impl fmt::Debug for ConnectSshRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ConnectSshRequest")
            .field("connection_id", &self.connection_id)
            .field("profile_version", &self.profile_version)
            .field("authentication", &"<redacted>")
            .field("jump", &self.jump)
            .finish()
    }
}

impl Drop for ConnectSshRequest {
    fn drop(&mut self) {
        zeroize_optional(&mut self.password_b64);
        zeroize_optional(&mut self.private_key_b64);
        zeroize_optional(&mut self.passphrase_b64);
    }
}

impl_json_request!(
    ConnectSshRequest,
    SshStatusResponse,
    Method::POST,
    |_: &ConnectSshRequest| "/v1/ssh/sessions".into(),
    StatusCode::CREATED
);

#[derive(Clone, Copy, Debug)]
pub struct DisconnectSshRequest {
    pub ssh_session_id: Uuid,
}

impl_empty_request!(
    DisconnectSshRequest,
    SshStatusResponse,
    Method::DELETE,
    |request: &DisconnectSshRequest| format!("/v1/ssh/sessions/{}", request.ssh_session_id),
    StatusCode::OK
);

#[derive(Clone, Copy, Debug, Serialize)]
pub struct OpenPtyRequest {
    pub ssh_session_id: Uuid,
    pub cols: u16,
    pub rows: u16,
}

impl_json_request!(
    OpenPtyRequest,
    PtySessionResponse,
    Method::POST,
    |_: &OpenPtyRequest| "/v1/pty/sessions".into(),
    StatusCode::CREATED
);

#[derive(Clone, Copy, Debug, Serialize)]
pub struct ResizePtyRequest {
    #[serde(skip)]
    pub pty_session_id: Uuid,
    pub cols: u16,
    pub rows: u16,
}

impl_json_request!(
    ResizePtyRequest,
    PtySessionResponse,
    Method::POST,
    |request: &ResizePtyRequest| format!("/v1/pty/sessions/{}/resize", request.pty_session_id),
    StatusCode::OK
);

#[derive(Clone, Copy, Debug)]
pub struct ClosePtyRequest {
    pub pty_session_id: Uuid,
}

impl_empty_request!(
    ClosePtyRequest,
    PtySessionResponse,
    Method::DELETE,
    |request: &ClosePtyRequest| format!("/v1/pty/sessions/{}", request.pty_session_id),
    StatusCode::OK
);

#[derive(Clone, Copy, Debug, Default)]
pub struct ListAgentApiConfigsRequest;

impl_empty_request!(
    ListAgentApiConfigsRequest,
    AgentApiConfigListResponse,
    Method::GET,
    |_: &ListAgentApiConfigsRequest| "/v1/agent/api-configs".into(),
    StatusCode::OK
);

#[derive(Clone, Copy, Debug)]
pub struct GetAgentApiConfigRequest {
    pub api_config_id: Uuid,
}

impl_empty_request!(
    GetAgentApiConfigRequest,
    AgentApiConfigResponse,
    Method::GET,
    |request: &GetAgentApiConfigRequest| format!("/v1/agent/api-configs/{}", request.api_config_id),
    StatusCode::OK
);

#[derive(Debug, Serialize)]
pub struct CreateAgentApiConfigRequest(pub ModelApiConfigInput);

impl_json_request!(
    CreateAgentApiConfigRequest,
    AgentApiConfigResponse,
    Method::POST,
    |_: &CreateAgentApiConfigRequest| "/v1/agent/api-configs".into(),
    StatusCode::CREATED
);

#[derive(Debug, Serialize)]
pub struct UpdateAgentApiConfigRequest {
    #[serde(skip)]
    pub api_config_id: Uuid,
    #[serde(flatten)]
    pub input: ModelApiConfigInput,
}

impl_json_request!(
    UpdateAgentApiConfigRequest,
    AgentApiConfigResponse,
    Method::PATCH,
    |request: &UpdateAgentApiConfigRequest| format!(
        "/v1/agent/api-configs/{}",
        request.api_config_id
    ),
    StatusCode::OK
);

#[derive(Clone, Copy, Debug)]
pub struct DeleteAgentApiConfigRequest {
    pub api_config_id: Uuid,
}

impl_empty_request!(
    DeleteAgentApiConfigRequest,
    DeleteResponse,
    Method::DELETE,
    |request: &DeleteAgentApiConfigRequest| format!(
        "/v1/agent/api-configs/{}",
        request.api_config_id
    ),
    StatusCode::OK
);

#[derive(Serialize)]
pub struct RunAgentTurnRequest {
    pub conversation_id: Option<Uuid>,
    pub ssh_session_id: Uuid,
    pub api_config_id: Uuid,
    pub api_key_credential_id: CredentialId,
    pub api_key_b64: String,
    pub user_message: String,
}

impl RunAgentTurnRequest {
    pub fn new(
        conversation_id: Option<Uuid>,
        ssh_session_id: Uuid,
        api_config_id: Uuid,
        api_key_credential_id: CredentialId,
        api_key_b64: String,
        user_message: String,
    ) -> Self {
        Self {
            conversation_id,
            ssh_session_id,
            api_config_id,
            api_key_credential_id,
            api_key_b64,
            user_message,
        }
    }
}

impl fmt::Debug for RunAgentTurnRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RunAgentTurnRequest")
            .field("conversation_id", &self.conversation_id)
            .field("ssh_session_id", &self.ssh_session_id)
            .field("api_config_id", &self.api_config_id)
            .field("api_key_credential_id", &self.api_key_credential_id)
            .field("api_key_b64", &"<redacted>")
            .field("user_message_bytes", &self.user_message.len())
            .finish()
    }
}

impl Drop for RunAgentTurnRequest {
    fn drop(&mut self) {
        self.api_key_b64.zeroize();
    }
}

impl_json_request!(
    RunAgentTurnRequest,
    AgentTurnResponse,
    Method::POST,
    |_: &RunAgentTurnRequest| "/v1/agent/turns".into(),
    StatusCode::OK
);

fn zeroize_optional(value: &mut Option<String>) {
    if let Some(value) = value {
        value.zeroize();
    }
}

fn json_body(value: &impl Serialize) -> Result<RuntimeRequestBody, RuntimeClientError> {
    let bytes = serde_json::to_vec(value).map_err(|_| RuntimeClientError::HttpContract {
        reason: "request serialization failed",
    })?;
    if bytes.len() > JSON_BODY_MAX_BYTES {
        return Err(RuntimeClientError::HttpRequestTooLarge);
    }
    Ok(RuntimeRequestBody::Json(bytes))
}
