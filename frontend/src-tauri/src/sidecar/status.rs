use serde::Serialize;
use time::OffsetDateTime;
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RuntimeState {
    Starting,
    Handshaking,
    Ready,
    Paused,
    Failed,
    Stopped,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct RuntimeStatus {
    pub state: RuntimeState,
    pub error_code: Option<String>,
    pub node: String,
    pub recoverable: bool,
    pub correlation_id: Uuid,
    pub last_sequence: u64,
    #[serde(with = "time::serde::rfc3339::option")]
    pub last_heartbeat_at: Option<OffsetDateTime>,
}

impl RuntimeStatus {
    pub fn starting(node: impl Into<String>) -> Self {
        Self {
            state: RuntimeState::Starting,
            error_code: None,
            node: node.into(),
            recoverable: false,
            correlation_id: Uuid::new_v4(),
            last_sequence: 0,
            last_heartbeat_at: None,
        }
    }
}
