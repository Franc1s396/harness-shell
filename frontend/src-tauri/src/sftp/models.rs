use serde::{de::Error as _, Deserialize, Deserializer, Serialize};
use uuid::Uuid;

pub const JS_SAFE_INTEGER_MAX: u64 = (1_u64 << 53) - 1;
pub const SFTP_CHUNK_BYTES: usize = 262_144;
pub const SFTP_SEQUENCE_MAX: u32 = i32::MAX as u32;

#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
#[error("{message}")]
pub struct ManualSftpError {
    code: String,
    message: String,
    origin: ManualSftpErrorOrigin,
    retained_operation_state: Option<RetainedOperationState>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ManualSftpErrorOrigin {
    Local,
    TrustedRemote,
    UncertainTransport,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RetainedOperationState {
    CleanupRequired,
    OutcomeUnknown,
}

impl ManualSftpError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            origin: ManualSftpErrorOrigin::Local,
            retained_operation_state: None,
        }
    }

    pub(crate) fn trusted_remote_with_state(
        code: impl Into<String>,
        message: impl Into<String>,
        retained_operation_state: Option<RetainedOperationState>,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            origin: ManualSftpErrorOrigin::TrustedRemote,
            retained_operation_state,
        }
    }

    pub(crate) fn uncertain_transport(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            origin: ManualSftpErrorOrigin::UncertainTransport,
            retained_operation_state: None,
        }
    }

    pub(crate) fn is_trusted_remote(&self) -> bool {
        self.origin == ManualSftpErrorOrigin::TrustedRemote
    }

    pub(crate) fn retained_operation_state(&self) -> Option<RetainedOperationState> {
        self.retained_operation_state
    }

    pub fn code(&self) -> &str {
        &self.code
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

pub fn require_js_safe(value: u64) -> Result<u64, ManualSftpError> {
    if value > JS_SAFE_INTEGER_MAX {
        return Err(ManualSftpError::new(
            "SFTP_FILE_SIZE_UNSUPPORTED",
            "The file size exceeds the supported desktop contract.",
        ));
    }
    Ok(value)
}

fn deserialize_js_safe<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let value = u64::deserialize(deserializer)?;
    require_js_safe(value).map_err(D::Error::custom)
}

fn deserialize_optional_js_safe<'de, D>(deserializer: D) -> Result<Option<u64>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<u64>::deserialize(deserializer)?;
    value
        .map(require_js_safe)
        .transpose()
        .map_err(D::Error::custom)
}

fn deserialize_optional_decimal_u64<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<String>::deserialize(deserializer)?;
    value
        .map(|value| {
            if value.is_empty()
                || !value.bytes().all(|byte| byte.is_ascii_digit())
                || value.parse::<u64>().is_err()
            {
                Err(D::Error::custom("expected a decimal uint64 string"))
            } else {
                Ok(value)
            }
        })
        .transpose()
}

fn validate_sha256<E: serde::de::Error>(value: &str) -> Result<(), E> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(E::custom("expected a lowercase SHA-256 hex string"));
    }
    Ok(())
}

fn deserialize_sha256<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let value = String::deserialize(deserializer)?;
    validate_sha256::<D::Error>(&value)?;
    Ok(value)
}

fn deserialize_optional_sha256<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<String>::deserialize(deserializer)?;
    if let Some(value) = value.as_deref() {
        validate_sha256::<D::Error>(value)?;
    }
    Ok(value)
}

fn deserialize_false<'de, D>(deserializer: D) -> Result<bool, D::Error>
where
    D: Deserializer<'de>,
{
    let value = bool::deserialize(deserializer)?;
    if value {
        return Err(D::Error::custom("expected false"));
    }
    Ok(false)
}

fn deserialize_sequence<'de, D>(deserializer: D) -> Result<u32, D::Error>
where
    D: Deserializer<'de>,
{
    let value = u32::deserialize(deserializer)?;
    if value > SFTP_SEQUENCE_MAX {
        return Err(D::Error::custom(
            "SFTP sequence exceeds the supported range",
        ));
    }
    Ok(value)
}

fn deserialize_sftp_version<'de, D>(deserializer: D) -> Result<u8, D::Error>
where
    D: Deserializer<'de>,
{
    let value = u8::deserialize(deserializer)?;
    if !(3..=6).contains(&value) {
        return Err(D::Error::custom("unsupported SFTP protocol version"));
    }
    Ok(value)
}

fn deserialize_true<'de, D>(deserializer: D) -> Result<bool, D::Error>
where
    D: Deserializer<'de>,
{
    let value = bool::deserialize(deserializer)?;
    if !value {
        return Err(D::Error::custom("expected true"));
    }
    Ok(true)
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EntryType {
    File,
    Directory,
    Symlink,
    Other,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ManualSftpContext {
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub home: String,
    pub host_label: String,
    #[serde(deserialize_with = "deserialize_sftp_version")]
    pub sftp_version: u8,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteEntry {
    pub name: String,
    pub path: String,
    pub entry_type: EntryType,
    #[serde(deserialize_with = "deserialize_optional_js_safe")]
    pub size: Option<u64>,
    pub mode: u64,
    #[serde(deserialize_with = "deserialize_optional_decimal_u64")]
    pub mtime_ns: Option<String>,
    pub link_target: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ListingBatch {
    pub listing_id: Uuid,
    pub path: String,
    #[serde(deserialize_with = "deserialize_listing_entries")]
    pub entries: Vec<RemoteEntry>,
    #[serde(deserialize_with = "deserialize_sequence")]
    pub next_sequence: u32,
    pub done: bool,
    #[serde(deserialize_with = "deserialize_observed_count")]
    pub observed_entry_count: u64,
    pub complete: bool,
}

fn deserialize_listing_entries<'de, D>(deserializer: D) -> Result<Vec<RemoteEntry>, D::Error>
where
    D: Deserializer<'de>,
{
    let entries = Vec::<RemoteEntry>::deserialize(deserializer)?;
    if entries.len() > 200 {
        return Err(D::Error::custom("listing batch exceeds 200 entries"));
    }
    Ok(entries)
}

fn deserialize_observed_count<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let value = u64::deserialize(deserializer)?;
    if value > 50_000 {
        return Err(D::Error::custom("listing exceeds 50000 entries"));
    }
    Ok(value)
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransferSnapshot {
    pub path: String,
    pub exists: bool,
    pub entry_type: Option<EntryType>,
    #[serde(deserialize_with = "deserialize_optional_js_safe")]
    pub size: Option<u64>,
    #[serde(deserialize_with = "deserialize_optional_decimal_u64")]
    pub mtime_ns: Option<String>,
    #[serde(deserialize_with = "deserialize_optional_sha256")]
    pub sha256: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteFileHash {
    pub path: String,
    pub snapshot: TransferSnapshot,
    #[serde(deserialize_with = "deserialize_sha256")]
    pub sha256: String,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub byte_count: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct UploadReady {
    pub operation_id: Uuid,
    pub temp_path: String,
    #[serde(deserialize_with = "deserialize_sequence")]
    pub next_sequence: u32,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub next_offset: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct UploadChunkAck {
    pub operation_id: Uuid,
    #[serde(deserialize_with = "deserialize_sequence")]
    pub next_sequence: u32,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub next_offset: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DownloadReady {
    pub operation_id: Uuid,
    pub path: String,
    pub snapshot: TransferSnapshot,
    #[serde(deserialize_with = "deserialize_sha256")]
    pub sha256: String,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub byte_count: u64,
    #[serde(deserialize_with = "deserialize_sequence")]
    pub next_sequence: u32,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub next_offset: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DownloadChunk {
    pub operation_id: Uuid,
    pub sequence: u32,
    pub offset: u64,
    pub bytes: bytes::Bytes,
    pub next_offset: u64,
    pub eof: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DeletePlanSummary {
    pub delete_plan_id: Uuid,
    pub operation_id: Uuid,
    pub root_path: String,
    pub root_snapshot: TransferSnapshot,
    pub file_count: u32,
    pub directory_count: u32,
    pub symlink_count: u32,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub total_byte_count: u64,
    #[serde(deserialize_with = "deserialize_sha256")]
    pub manifest_sha256: String,
    #[serde(deserialize_with = "deserialize_true")]
    pub complete: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationTerminalState {
    Succeeded,
    Failed,
    Cancelled,
    CleanupRequired,
    OutcomeUnknown,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OperationTerminalProjection {
    pub operation_id: Uuid,
    pub state: OperationTerminalState,
    pub error_code: Option<String>,
    pub message: String,
    #[serde(deserialize_with = "deserialize_optional_sha256")]
    pub sha256: Option<String>,
    #[serde(deserialize_with = "deserialize_optional_js_safe")]
    pub byte_count: Option<u64>,
    pub recovery_id: Option<Uuid>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryKind {
    UploadTemp,
    DownloadPart,
    DeleteTombstone,
    MutationUnknown,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryState {
    CleanupRequired,
    OutcomeUnknown,
    RecoveryRequired,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryAction {
    Verify,
    DeleteTemp,
    ContinueDelete,
    RestoreTombstone,
    OpenLocalFolder,
    Keep,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoverySummary {
    pub recovery_id: Uuid,
    pub operation_id: Uuid,
    pub kind: RecoveryKind,
    pub host_label: String,
    pub remote_path: Option<String>,
    pub display_name: String,
    pub state: RecoveryState,
    pub created_at: String,
    pub available_actions: Vec<RecoveryAction>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MutationKind {
    Mkdir,
    Rename,
    Remove,
    RecursiveDelete,
    Recovery,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MutationPhase {
    Preparing,
    Isolating,
    Deleting,
    Cleaning,
    Committing,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MutationProgressProjection {
    pub operation_id: Uuid,
    pub kind: MutationKind,
    pub phase: MutationPhase,
    pub display_name: String,
    pub remote_path: String,
    pub host_label: String,
    #[serde(deserialize_with = "deserialize_optional_js_safe")]
    pub items_completed: Option<u64>,
    #[serde(deserialize_with = "deserialize_optional_js_safe")]
    pub items_total: Option<u64>,
    #[serde(deserialize_with = "deserialize_false")]
    pub cancellable: bool,
}

/// Transfer direction exposed to the WebView progress projection.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TransferDirection {
    Upload,
    Download,
}

/// Coordinator-owned lifecycle phase. `Committing` is deliberately non-cancellable.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationPhase {
    Preparing,
    Transferring,
    Verifying,
    Committing,
}

/// Safe transfer progress projection for the main window.
///
/// Local file paths intentionally do not appear in this serializable type.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransferProgressProjection {
    pub operation_id: Uuid,
    pub direction: TransferDirection,
    pub phase: OperationPhase,
    pub display_name: String,
    pub remote_path: String,
    pub host_label: String,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub bytes_completed: u64,
    #[serde(deserialize_with = "deserialize_js_safe")]
    pub bytes_total: u64,
    pub cancellable: bool,
}
