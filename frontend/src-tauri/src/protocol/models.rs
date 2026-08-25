use std::fmt;

use serde::{de::Error as _, Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};
use time::OffsetDateTime;
use uuid::Uuid;
use zeroize::Zeroizing;

pub const PROTOCOL_VERSION: u8 = 1;
pub const MAX_HEADER_BYTES: usize = 8_192;
pub const MAX_PAYLOAD_BYTES: usize = 1_048_576;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MessageType {
    Request,
    Response,
    Event,
    Error,
    Cancel,
    Heartbeat,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Sensitivity {
    Normal,
    Secret,
}

#[derive(Clone, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FrameEnvelope {
    #[serde(deserialize_with = "deserialize_protocol_version")]
    pub protocol_version: u8,
    pub message_type: MessageType,
    pub request_id: Uuid,
    pub task_id: Option<Uuid>,
    pub workflow_run_id: Option<Uuid>,
    #[serde(deserialize_with = "deserialize_positive_sequence")]
    pub sequence: u64,
    #[serde(with = "time::serde::rfc3339")]
    pub timestamp: OffsetDateTime,
    pub sensitivity: Sensitivity,
    pub payload: Map<String, Value>,
}

impl FrameEnvelope {
    pub fn redacted_debug(&self) -> RedactedFrameDebug<'_> {
        RedactedFrameDebug(self)
    }
}

impl fmt::Debug for FrameEnvelope {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.redacted_debug().fmt(formatter)
    }
}

pub struct RedactedFrameDebug<'a>(&'a FrameEnvelope);

impl fmt::Debug for RedactedFrameDebug<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let frame = self.0;
        let mut debug = formatter.debug_struct("FrameEnvelope");
        debug
            .field("protocol_version", &frame.protocol_version)
            .field("message_type", &frame.message_type)
            .field("request_id", &frame.request_id)
            .field("task_id", &frame.task_id)
            .field("workflow_run_id", &frame.workflow_run_id)
            .field("sequence", &frame.sequence)
            .field("timestamp", &frame.timestamp)
            .field("sensitivity", &frame.sensitivity);

        match frame.sensitivity {
            Sensitivity::Secret => {
                let payload_bytes = serde_json::to_vec(&frame.payload)
                    .map(Zeroizing::new)
                    .map(|payload| payload.len())
                    .unwrap_or(0);
                debug.field("payload_bytes", &payload_bytes);
            }
            Sensitivity::Normal => {
                let mut payload_fields: Vec<&str> =
                    frame.payload.keys().map(String::as_str).collect();
                payload_fields.sort_unstable();
                debug.field("payload_fields", &payload_fields);
            }
        }
        debug.finish()
    }
}

fn deserialize_protocol_version<'de, D>(deserializer: D) -> Result<u8, D::Error>
where
    D: Deserializer<'de>,
{
    let version = u8::deserialize(deserializer)?;
    if version != PROTOCOL_VERSION {
        return Err(D::Error::custom("unsupported protocol version"));
    }
    Ok(version)
}

fn deserialize_positive_sequence<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let sequence = u64::deserialize(deserializer)?;
    if sequence == 0 {
        return Err(D::Error::custom("sequence must be positive"));
    }
    Ok(sequence)
}
