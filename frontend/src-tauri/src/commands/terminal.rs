use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use tauri::State;
use uuid::Uuid;

use crate::runtime::{
    ClosePtyRequest, OpenPtyRequest, PtyInput, ResizePtyRequest, RuntimeClient, RuntimeClientHandle,
};

use super::CommandError;

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

#[tauri::command]
pub async fn open_pty(
    runtime: State<'_, RuntimeClientHandle>,
    ssh_session_id: Uuid,
    cols: u16,
    rows: u16,
) -> Result<PtySession, CommandError> {
    open_pty_with_runtime(&*runtime, ssh_session_id, cols, rows).await
}

#[doc(hidden)]
pub async fn open_pty_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    ssh_session_id: Uuid,
    cols: u16,
    rows: u16,
) -> Result<PtySession, CommandError> {
    validate_size(cols, rows)?;
    runtime
        .execute(OpenPtyRequest {
            ssh_session_id,
            cols,
            rows,
        })
        .await
        .map(|response| response.pty_session)
        .map_err(super::connections::map_runtime_error)
}

#[tauri::command]
pub async fn write_pty(
    runtime: State<'_, RuntimeClientHandle>,
    pty_session_id: Uuid,
    data_b64: String,
) -> Result<usize, CommandError> {
    write_pty_with_runtime(&*runtime, pty_session_id, data_b64).await
}

#[doc(hidden)]
pub async fn write_pty_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    pty_session_id: Uuid,
    data_b64: String,
) -> Result<usize, CommandError> {
    let data = validate_pty_input(&data_b64)?;
    let request = PtyInput::new(pty_session_id, &data).map_err(|_| {
        CommandError::new(
            "INVALID_PTY_INPUT",
            "PTY input must contain 1..32768 bytes.",
        )
    })?;
    runtime
        .send_pty_input(request)
        .await
        .map_err(super::connections::map_runtime_error)
        .and_then(|result| {
            if result.pty_session_id == pty_session_id
                && result.accepted_bytes as usize == data.len()
            {
                Ok(result.accepted_bytes as usize)
            } else {
                Err(CommandError::new(
                    "RUNTIME_WEBSOCKET_CONTRACT_FAILED",
                    "The PTY runtime returned an invalid byte count.",
                ))
            }
        })
}

#[tauri::command]
pub async fn resize_pty(
    runtime: State<'_, RuntimeClientHandle>,
    pty_session_id: Uuid,
    cols: u16,
    rows: u16,
) -> Result<PtySession, CommandError> {
    resize_pty_with_runtime(&*runtime, pty_session_id, cols, rows).await
}

#[doc(hidden)]
pub async fn resize_pty_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    pty_session_id: Uuid,
    cols: u16,
    rows: u16,
) -> Result<PtySession, CommandError> {
    validate_size(cols, rows)?;
    runtime
        .execute(ResizePtyRequest {
            pty_session_id,
            cols,
            rows,
        })
        .await
        .map(|response| response.pty_session)
        .map_err(super::connections::map_runtime_error)
}

#[tauri::command]
pub async fn close_pty(
    runtime: State<'_, RuntimeClientHandle>,
    pty_session_id: Uuid,
) -> Result<PtySession, CommandError> {
    close_pty_with_runtime(&*runtime, pty_session_id).await
}

#[doc(hidden)]
pub async fn close_pty_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    pty_session_id: Uuid,
) -> Result<PtySession, CommandError> {
    runtime
        .execute(ClosePtyRequest { pty_session_id })
        .await
        .map(|response| response.pty_session)
        .map_err(super::connections::map_runtime_error)
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
