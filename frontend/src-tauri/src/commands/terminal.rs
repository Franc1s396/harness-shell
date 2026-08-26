use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use tauri::State;
use uuid::Uuid;

use crate::sidecar::broker::RuntimeBrokerHandle;

use super::{
    connections::{request, request_secret},
    CommandError,
};

const MAX_PTY_CHUNK_BYTES: usize = 32_768;

#[derive(Debug, Deserialize, Serialize)]
pub struct PtySession {
    pub pty_session_id: Uuid,
    pub ssh_session_id: Uuid,
    pub connection_id: Uuid,
    pub cols: u16,
    pub rows: u16,
    pub state: PtyState,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PtyState {
    Open,
    Closed,
    Failed,
}

#[derive(Deserialize)]
struct PtySessionResult {
    pty_session: PtySession,
}

#[derive(Deserialize)]
struct WriteResult {
    accepted_bytes: usize,
}

#[tauri::command]
pub async fn open_pty(
    broker: State<'_, RuntimeBrokerHandle>,
    ssh_session_id: Uuid,
    cols: u16,
    rows: u16,
) -> Result<PtySession, CommandError> {
    validate_size(cols, rows)?;
    let params = Map::from_iter([
        (
            "ssh_session_id".to_owned(),
            Value::String(ssh_session_id.to_string()),
        ),
        ("cols".to_owned(), Value::from(cols)),
        ("rows".to_owned(), Value::from(rows)),
    ]);
    request(&broker, "pty.open", params)
        .await
        .map(|result: PtySessionResult| result.pty_session)
}

#[tauri::command]
pub async fn write_pty(
    broker: State<'_, RuntimeBrokerHandle>,
    pty_session_id: Uuid,
    data_b64: String,
) -> Result<usize, CommandError> {
    let data = validate_pty_input(&data_b64)?;
    let params = Map::from_iter([
        (
            "pty_session_id".to_owned(),
            Value::String(pty_session_id.to_string()),
        ),
        ("data_b64".to_owned(), Value::String(data_b64)),
    ]);
    request_secret(&broker, "pty.write", params)
        .await
        .and_then(|result: WriteResult| {
            if result.accepted_bytes == data.len() {
                Ok(result.accepted_bytes)
            } else {
                Err(CommandError::new(
                    "SIDECAR_RESPONSE_INVALID",
                    "The PTY runtime returned an invalid byte count.",
                ))
            }
        })
}

#[tauri::command]
pub async fn resize_pty(
    broker: State<'_, RuntimeBrokerHandle>,
    pty_session_id: Uuid,
    cols: u16,
    rows: u16,
) -> Result<PtySession, CommandError> {
    validate_size(cols, rows)?;
    let params = Map::from_iter([
        (
            "pty_session_id".to_owned(),
            Value::String(pty_session_id.to_string()),
        ),
        ("cols".to_owned(), Value::from(cols)),
        ("rows".to_owned(), Value::from(rows)),
    ]);
    request(&broker, "pty.resize", params)
        .await
        .map(|result: PtySessionResult| result.pty_session)
}

#[tauri::command]
pub async fn close_pty(
    broker: State<'_, RuntimeBrokerHandle>,
    pty_session_id: Uuid,
) -> Result<PtySession, CommandError> {
    let params = Map::from_iter([(
        "pty_session_id".to_owned(),
        Value::String(pty_session_id.to_string()),
    )]);
    request(&broker, "pty.close", params)
        .await
        .map(|result: PtySessionResult| result.pty_session)
}

fn validate_size(cols: u16, rows: u16) -> Result<(), CommandError> {
    if !(20..=500).contains(&cols) || !(5..=300).contains(&rows) {
        return Err(CommandError::new(
            "PTY_SIZE_INVALID",
            "PTY dimensions are outside the supported range.",
        ));
    }
    Ok(())
}

fn validate_pty_input(encoded: &str) -> Result<Vec<u8>, CommandError> {
    let decoded = STANDARD.decode(encoded).map_err(|_| {
        CommandError::new("INVALID_PTY_INPUT", "PTY input is not canonical base64.")
    })?;
    if decoded.is_empty()
        || decoded.len() > MAX_PTY_CHUNK_BYTES
        || STANDARD.encode(&decoded) != encoded
    {
        return Err(CommandError::new(
            "INVALID_PTY_INPUT",
            "PTY input must contain 1..32768 bytes.",
        ));
    }
    Ok(decoded)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_control_sequences_are_forwarded_as_opaque_bytes() {
        let bytes = b"\x1b]0;title\x07\x1b]8;;https://example.test\x07link\x1b]8;;\x07\x1b]52;c;YQ==\x07\x1b[2J";
        let encoded = STANDARD.encode(bytes);
        assert_eq!(validate_pty_input(&encoded).unwrap(), bytes);
    }

    #[test]
    fn pty_input_and_dimensions_are_bounded() {
        assert!(validate_pty_input("").is_err());
        assert!(validate_pty_input(&STANDARD.encode(vec![0; 32_769])).is_err());
        assert!(validate_size(19, 24).is_err());
        assert!(validate_size(80, 301).is_err());
    }
}
