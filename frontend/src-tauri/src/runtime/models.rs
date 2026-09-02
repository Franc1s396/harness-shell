use std::{fmt, path::Path};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use time::OffsetDateTime;
use uuid::Uuid;
use zeroize::Zeroize;

use crate::{
    commands::{
        AgentRunStatus, ConnectionProfile, ConnectionStatus as CommandConnectionStatus,
        HostKeyRecord, ModelApiConfig, PtySession,
    },
    sftp::models::MutationProgressProjection,
};

pub const JSON_BODY_MAX_BYTES: usize = 1_048_576;
pub const HEARTBEAT_INTERVAL_MS: u64 = 5_000;
pub const HEARTBEAT_TIMEOUT_MS: u64 = 15_000;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RuntimePhase {
    LiveNotInitialized,
    Initializing,
    Ready,
    Draining,
    Converging,
    Closing,
    Stopped,
    Failed,
}

impl RuntimePhase {
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::LiveNotInitialized => "LIVE_NOT_INITIALIZED",
            Self::Initializing => "INITIALIZING",
            Self::Ready => "READY",
            Self::Draining => "DRAINING",
            Self::Converging => "CONVERGING",
            Self::Closing => "CLOSING",
            Self::Stopped => "STOPPED",
            Self::Failed => "FAILED",
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ProblemDetails {
    #[serde(rename = "type")]
    pub problem_type: String,
    pub title: String,
    pub status: u16,
    pub error_code: String,
    pub message: String,
    pub request_id: Uuid,
    pub details: Map<String, Value>,
}

pub trait CorrelatedResponse {
    fn request_id(&self) -> Uuid;
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct HealthLiveResponse {
    pub request_id: Uuid,
    pub live: bool,
}

impl CorrelatedResponse for HealthLiveResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct HealthReadyResponse {
    pub request_id: Uuid,
    pub ready: bool,
    pub state: RuntimePhase,
}

impl CorrelatedResponse for HealthReadyResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeStateResponse {
    pub request_id: Uuid,
    pub state: RuntimePhase,
}

impl CorrelatedResponse for RuntimeStateResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RequestCancelResponse {
    pub request_id: Uuid,
    pub target_request_id: Uuid,
    pub cancellation_requested: bool,
}

impl CorrelatedResponse for RequestCancelResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionListResponse {
    pub request_id: Uuid,
    pub connections: Vec<ConnectionProfile>,
}

impl CorrelatedResponse for ConnectionListResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionResponse {
    pub request_id: Uuid,
    pub connection: ConnectionProfile,
}

impl CorrelatedResponse for ConnectionResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeleteResponse {
    pub request_id: Uuid,
    pub deleted: bool,
}

impl CorrelatedResponse for DeleteResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostKeyResponse {
    pub request_id: Uuid,
    pub host_key: HostKeyRecord,
}

impl CorrelatedResponse for HostKeyResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SshStatusResponse {
    pub request_id: Uuid,
    pub status: CommandConnectionStatus,
}

impl CorrelatedResponse for SshStatusResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PtySessionResponse {
    pub request_id: Uuid,
    pub pty_session: PtySession,
}

impl CorrelatedResponse for PtySessionResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentApiConfigListResponse {
    pub request_id: Uuid,
    pub configs: Vec<ModelApiConfig>,
}

impl CorrelatedResponse for AgentApiConfigListResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentApiConfigResponse {
    pub request_id: Uuid,
    pub config: ModelApiConfig,
}

impl CorrelatedResponse for AgentApiConfigResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentTurnResponse {
    pub request_id: Uuid,
    pub conversation_id: Uuid,
    pub agent_run_id: Uuid,
    pub status: AgentRunStatus,
    pub final_text: Option<String>,
    pub react_iteration: u8,
    pub error_code: Option<String>,
}

impl CorrelatedResponse for AgentTurnResponse {
    fn request_id(&self) -> Uuid {
        self.request_id
    }
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeInitializeBody {
    pub app_version: String,
    pub runtime_db_path: String,
    pub runtime_data_key_b64: String,
    pub audit_hmac_key_b64: String,
    pub heartbeat_interval_ms: u64,
    pub heartbeat_timeout_ms: u64,
}

impl RuntimeInitializeBody {
    pub fn new(
        app_version: impl Into<String>,
        runtime_db_path: impl Into<String>,
        runtime_data_key_b64: impl Into<String>,
        audit_hmac_key_b64: impl Into<String>,
        heartbeat_interval_ms: u64,
        heartbeat_timeout_ms: u64,
    ) -> Result<Self, RuntimeModelError> {
        let runtime_db_path = runtime_db_path.into();
        let runtime_data_key_b64 = runtime_data_key_b64.into();
        let audit_hmac_key_b64 = audit_hmac_key_b64.into();
        if !Path::new(&runtime_db_path).is_absolute() {
            return Err(RuntimeModelError::InvalidRuntimePath);
        }
        validate_key(&runtime_data_key_b64)?;
        validate_key(&audit_hmac_key_b64)?;
        if heartbeat_interval_ms != HEARTBEAT_INTERVAL_MS
            || heartbeat_timeout_ms != HEARTBEAT_TIMEOUT_MS
        {
            return Err(RuntimeModelError::InvalidHeartbeat);
        }
        Ok(Self {
            app_version: app_version.into(),
            runtime_db_path,
            runtime_data_key_b64,
            audit_hmac_key_b64,
            heartbeat_interval_ms,
            heartbeat_timeout_ms,
        })
    }
}

impl fmt::Debug for RuntimeInitializeBody {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RuntimeInitializeBody")
            .field("app_version", &self.app_version)
            .field("runtime_db_path", &self.runtime_db_path)
            .field("runtime_data_key_b64", &"<redacted>")
            .field("audit_hmac_key_b64", &"<redacted>")
            .field("heartbeat_interval_ms", &self.heartbeat_interval_ms)
            .field("heartbeat_timeout_ms", &self.heartbeat_timeout_ms)
            .finish()
    }
}

impl Drop for RuntimeInitializeBody {
    fn drop(&mut self) {
        self.runtime_data_key_b64.zeroize();
        self.audit_hmac_key_b64.zeroize();
    }
}

#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
pub enum RuntimeModelError {
    #[error("runtime database path must be absolute")]
    InvalidRuntimePath,
    #[error("runtime key must be canonical base64 for exactly 32 bytes")]
    InvalidRuntimeKey,
    #[error("runtime heartbeat values do not match the frozen contract")]
    InvalidHeartbeat,
}

fn validate_key(value: &str) -> Result<(), RuntimeModelError> {
    let decoded = STANDARD
        .decode(value)
        .map_err(|_| RuntimeModelError::InvalidRuntimeKey)?;
    if decoded.len() != 32 || STANDARD.encode(decoded) != value {
        return Err(RuntimeModelError::InvalidRuntimeKey);
    }
    Ok(())
}

pub fn now_utc() -> OffsetDateTime {
    OffsetDateTime::now_utc()
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConnectionState {
    Disconnected,
    Connecting,
    HostKeyRequired,
    Ready,
    Closing,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostKeyCandidate {
    pub connection_id: Uuid,
    pub host: String,
    pub port: u16,
    pub key_algorithm: String,
    pub fingerprint_sha256: String,
    pub public_key_openssh_b64: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionStatus {
    pub connection_id: Uuid,
    pub state: ConnectionState,
    pub session_id: Option<Uuid>,
    pub error_code: Option<String>,
    pub recoverable: bool,
    pub correlation_id: Uuid,
    pub host_key_candidate: Option<HostKeyCandidate>,
    pub trusted_fingerprint_sha256: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PtyInputResultPayload {
    pub pty_session_id: Uuid,
    pub accepted_bytes: u32,
    pub error_code: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PtyOutputPayload {
    pub pty_session_id: Uuid,
    pub data_b64: String,
    pub stream_sequence: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PtyClosedPayload {
    pub pty_session_id: Uuid,
    pub exit_status: Option<i32>,
    pub exit_signal: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RuntimePongPayload {
    #[serde(with = "time::serde::rfc3339")]
    pub server_timestamp: OffsetDateTime,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeErrorPayload {
    pub error_code: String,
    pub message: String,
    pub details: Option<Map<String, Value>>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", deny_unknown_fields)]
pub enum RuntimeServerMessage {
    #[serde(rename = "pty.input_result")]
    PtyInputResult {
        #[serde(deserialize_with = "deserialize_schema_version")]
        schema_version: u8,
        message_id: Uuid,
        causation_id: Uuid,
        #[serde(with = "time::serde::rfc3339")]
        timestamp: OffsetDateTime,
        payload: PtyInputResultPayload,
    },
    #[serde(rename = "pty.output")]
    PtyOutput {
        #[serde(deserialize_with = "deserialize_schema_version")]
        schema_version: u8,
        message_id: Uuid,
        causation_id: (),
        #[serde(with = "time::serde::rfc3339")]
        timestamp: OffsetDateTime,
        payload: PtyOutputPayload,
    },
    #[serde(rename = "pty.closed")]
    PtyClosed {
        #[serde(deserialize_with = "deserialize_schema_version")]
        schema_version: u8,
        message_id: Uuid,
        causation_id: (),
        #[serde(with = "time::serde::rfc3339")]
        timestamp: OffsetDateTime,
        payload: PtyClosedPayload,
    },
    #[serde(rename = "ssh.connection_state")]
    SshConnectionState {
        #[serde(deserialize_with = "deserialize_schema_version")]
        schema_version: u8,
        message_id: Uuid,
        causation_id: (),
        #[serde(with = "time::serde::rfc3339")]
        timestamp: OffsetDateTime,
        payload: ConnectionStatus,
    },
    #[serde(rename = "sftp.operation_progress")]
    SftpOperationProgress {
        #[serde(deserialize_with = "deserialize_schema_version")]
        schema_version: u8,
        message_id: Uuid,
        causation_id: (),
        #[serde(with = "time::serde::rfc3339")]
        timestamp: OffsetDateTime,
        payload: MutationProgressProjection,
    },
    #[serde(rename = "runtime.pong")]
    RuntimePong {
        #[serde(deserialize_with = "deserialize_schema_version")]
        schema_version: u8,
        message_id: Uuid,
        causation_id: Uuid,
        #[serde(with = "time::serde::rfc3339")]
        timestamp: OffsetDateTime,
        payload: RuntimePongPayload,
    },
    #[serde(rename = "runtime.error")]
    RuntimeError {
        #[serde(deserialize_with = "deserialize_schema_version")]
        schema_version: u8,
        message_id: Uuid,
        causation_id: Option<Uuid>,
        #[serde(with = "time::serde::rfc3339")]
        timestamp: OffsetDateTime,
        payload: RuntimeErrorPayload,
    },
}

fn deserialize_schema_version<'de, D>(deserializer: D) -> Result<u8, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = u8::deserialize(deserializer)?;
    if value != 1 {
        return Err(serde::de::Error::custom("runtime schema version must be 1"));
    }
    Ok(value)
}
