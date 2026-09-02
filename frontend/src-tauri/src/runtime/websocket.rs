use std::{collections::HashMap, fmt, time::Duration};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use serde_json::{json, Value};
use tokio::{
    sync::{mpsc, oneshot},
    task::JoinHandle,
    time::Instant,
};
use tokio_tungstenite::{connect_async, tungstenite::Message, MaybeTlsStream, WebSocketStream};
use uuid::Uuid;

use super::client::RuntimeClientError;
use super::models::{
    now_utc, ConnectionStatus, PtyClosedPayload, PtyInputResultPayload, PtyOutputPayload,
    RuntimeErrorPayload, RuntimeServerMessage,
};

pub const WEBSOCKET_QUEUE_CAPACITY: usize = 64;
pub const MAX_WEBSOCKET_TEXT_BYTES: usize = 65_536;
pub const MAX_PTY_BYTES: usize = 32_768;
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(15);
const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);

type RuntimeSocket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;

pub struct PtyInput {
    pub pty_session_id: Uuid,
    data: Vec<u8>,
}

impl PtyInput {
    pub fn new(pty_session_id: Uuid, data: &[u8]) -> Result<Self, RuntimeWebSocketError> {
        if !(1..=MAX_PTY_BYTES).contains(&data.len()) {
            return Err(RuntimeWebSocketError::Contract {
                reason: "PTY input length is invalid",
            });
        }
        Ok(Self {
            pty_session_id,
            data: data.to_vec(),
        })
    }

    pub fn data_len(&self) -> usize {
        self.data.len()
    }
}

impl fmt::Debug for PtyInput {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PtyInput")
            .field("pty_session_id", &self.pty_session_id)
            .field("data_bytes", &self.data.len())
            .finish()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PtyInputResult {
    pub pty_session_id: Uuid,
    pub accepted_bytes: u32,
}

pub struct RuntimeProjection {
    pub event: &'static str,
    pub payload: Value,
}

pub enum RuntimeWebSocketCommand {
    PtyInput {
        request: PtyInput,
        reply: oneshot::Sender<Result<PtyInputResult, RuntimeClientError>>,
    },
    Shutdown,
}

#[derive(Clone)]
pub struct RuntimeWebSocketHandle {
    sender: mpsc::Sender<RuntimeWebSocketCommand>,
}

impl RuntimeWebSocketHandle {
    pub async fn send_pty_input(
        &self,
        request: PtyInput,
    ) -> Result<PtyInputResult, RuntimeClientError> {
        let (reply, receiver) = oneshot::channel();
        self.sender
            .send(RuntimeWebSocketCommand::PtyInput { request, reply })
            .await
            .map_err(|_| RuntimeClientError::WebSocketClosed)?;
        receiver
            .await
            .map_err(|_| RuntimeClientError::WebSocketClosed)?
    }

    pub async fn shutdown(&self) -> Result<(), RuntimeClientError> {
        self.sender
            .send(RuntimeWebSocketCommand::Shutdown)
            .await
            .map_err(|_| RuntimeClientError::WebSocketClosed)
    }
}

pub struct RuntimeWebSocketConnection {
    handle: RuntimeWebSocketHandle,
    task: JoinHandle<Result<(), RuntimeWebSocketError>>,
}

impl RuntimeWebSocketConnection {
    pub async fn connect(
        port: u16,
        projection_tx: mpsc::Sender<RuntimeProjection>,
    ) -> Result<Self, RuntimeWebSocketError> {
        if port == 0 {
            return Err(RuntimeWebSocketError::Configuration);
        }
        let url = format!("ws://127.0.0.1:{port}/v1/runtime/events");
        let (mut socket, response) = connect_async(url)
            .await
            .map_err(|_| RuntimeWebSocketError::Connect)?;
        if response.status().as_u16() != 101 {
            return Err(RuntimeWebSocketError::Connect);
        }

        let mut state = ActorState::new(projection_tx);
        let initial_ping = state.send_ping(&mut socket).await?;
        tokio::time::timeout(
            STARTUP_TIMEOUT,
            state.wait_for_initial_pong(&mut socket, initial_ping),
        )
        .await
        .map_err(|_| RuntimeWebSocketError::HeartbeatTimeout)??;

        let (sender, receiver) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);
        let handle = RuntimeWebSocketHandle { sender };
        let task = tokio::spawn(state.run(socket, receiver));
        Ok(Self { handle, task })
    }

    pub fn split(
        self,
    ) -> (
        RuntimeWebSocketHandle,
        JoinHandle<Result<(), RuntimeWebSocketError>>,
    ) {
        (self.handle, self.task)
    }
}

struct PendingPtyInput {
    pty_session_id: Uuid,
    byte_count: u32,
    reply: oneshot::Sender<Result<PtyInputResult, RuntimeClientError>>,
}

struct ActorState {
    projection_tx: mpsc::Sender<RuntimeProjection>,
    pending_inputs: HashMap<Uuid, PendingPtyInput>,
    pending_pings: HashMap<Uuid, Instant>,
    next_pty_sequence: HashMap<Uuid, u64>,
    last_valid_pong: Instant,
}

impl ActorState {
    fn new(projection_tx: mpsc::Sender<RuntimeProjection>) -> Self {
        Self {
            projection_tx,
            pending_inputs: HashMap::new(),
            pending_pings: HashMap::new(),
            next_pty_sequence: HashMap::new(),
            last_valid_pong: Instant::now(),
        }
    }

    async fn wait_for_initial_pong(
        &mut self,
        socket: &mut RuntimeSocket,
        initial_ping: Uuid,
    ) -> Result<(), RuntimeWebSocketError> {
        loop {
            let message = socket
                .next()
                .await
                .ok_or(RuntimeWebSocketError::Disconnected)?
                .map_err(|_| RuntimeWebSocketError::Disconnected)?;
            let server_message = decode_text(message)?;
            if matches!(
                &server_message,
                RuntimeServerMessage::RuntimePong { causation_id, .. }
                    if *causation_id == initial_ping
            ) {
                self.handle_server_message(server_message).await?;
                return Ok(());
            }
            self.handle_server_message(server_message).await?;
        }
    }

    async fn run(
        mut self,
        mut socket: RuntimeSocket,
        mut commands: mpsc::Receiver<RuntimeWebSocketCommand>,
    ) -> Result<(), RuntimeWebSocketError> {
        let mut heartbeat =
            tokio::time::interval_at(Instant::now() + HEARTBEAT_INTERVAL, HEARTBEAT_INTERVAL);
        loop {
            let deadline = self.last_valid_pong + HEARTBEAT_TIMEOUT;
            tokio::select! {
                command = commands.recv() => match command {
                    Some(RuntimeWebSocketCommand::PtyInput { request, reply }) => {
                        self.send_pty_input(&mut socket, request, reply).await?;
                    }
                    Some(RuntimeWebSocketCommand::Shutdown) | None => {
                        self.fail_pending(RuntimeClientError::WebSocketClosed);
                        let _ = socket.close(None).await;
                        return Ok(());
                    }
                },
                message = socket.next() => {
                    let message = message
                        .ok_or(RuntimeWebSocketError::Disconnected)?
                        .map_err(|_| RuntimeWebSocketError::Disconnected)?;
                    let server_message = match decode_text(message) {
                        Ok(server_message) => server_message,
                        Err(error) => {
                            // Close every pending caller before the actor exposes the
                            // protocol-level failure to its supervisor.
                            self.fail_pending(RuntimeClientError::WebSocketClosed);
                            return Err(error);
                        }
                    };
                    if let Err(error) = self.handle_server_message(server_message).await {
                        self.fail_pending(RuntimeClientError::WebSocketClosed);
                        return Err(error);
                    }
                }
                _ = heartbeat.tick() => {
                    self.send_ping(&mut socket).await?;
                }
                _ = tokio::time::sleep_until(deadline) => {
                    self.fail_pending(RuntimeClientError::WebSocketClosed);
                    return Err(RuntimeWebSocketError::HeartbeatTimeout);
                }
            }
        }
    }

    async fn send_ping(
        &mut self,
        socket: &mut RuntimeSocket,
    ) -> Result<Uuid, RuntimeWebSocketError> {
        let message_id = Uuid::new_v4();
        let message = RuntimeClientMessage::RuntimePing {
            schema_version: 1,
            message_id,
            causation_id: (),
            timestamp: now_utc(),
            payload: RuntimePingPayload {
                client_timestamp: now_utc(),
            },
        };
        send_json(socket, &message).await?;
        self.pending_pings.insert(message_id, Instant::now());
        Ok(message_id)
    }

    async fn send_pty_input(
        &mut self,
        socket: &mut RuntimeSocket,
        request: PtyInput,
        reply: oneshot::Sender<Result<PtyInputResult, RuntimeClientError>>,
    ) -> Result<(), RuntimeWebSocketError> {
        if self.pending_inputs.len() >= WEBSOCKET_QUEUE_CAPACITY {
            let _ = reply.send(Err(RuntimeClientError::WebSocketCapacity));
            return Ok(());
        }
        let message_id = Uuid::new_v4();
        let byte_count = request.data.len() as u32;
        let message = RuntimeClientMessage::PtyInput {
            schema_version: 1,
            message_id,
            causation_id: (),
            timestamp: now_utc(),
            payload: PtyInputPayload {
                pty_session_id: request.pty_session_id,
                data_b64: STANDARD.encode(&request.data),
            },
        };
        send_json(socket, &message).await?;
        self.pending_inputs.insert(
            message_id,
            PendingPtyInput {
                pty_session_id: request.pty_session_id,
                byte_count,
                reply,
            },
        );
        Ok(())
    }

    async fn handle_server_message(
        &mut self,
        message: RuntimeServerMessage,
    ) -> Result<(), RuntimeWebSocketError> {
        match message {
            RuntimeServerMessage::PtyInputResult {
                causation_id,
                payload,
                ..
            } => {
                self.complete_pty_input(causation_id, payload)?;
            }
            RuntimeServerMessage::PtyOutput { payload, .. } => {
                validate_base64(&payload.data_b64)?;
                let expected = self
                    .next_pty_sequence
                    .entry(payload.pty_session_id)
                    .or_insert(1);
                if payload.stream_sequence != *expected {
                    return Err(RuntimeWebSocketError::SequenceGap);
                }
                *expected += 1;
                self.project_pty_output(payload).await;
            }
            RuntimeServerMessage::PtyClosed { payload, .. } => {
                self.next_pty_sequence.remove(&payload.pty_session_id);
                self.project_pty_closed(payload).await;
            }
            RuntimeServerMessage::SshConnectionState { payload, .. } => {
                self.project_ssh(payload).await;
            }
            RuntimeServerMessage::SftpOperationProgress { payload, .. } => {
                self.project(
                    "manual-sftp://operation-state",
                    serde_json::to_value(payload).map_err(|_| RuntimeWebSocketError::Contract {
                        reason: "SFTP projection serialization failed",
                    })?,
                )
                .await;
            }
            RuntimeServerMessage::RuntimePong { causation_id, .. } => {
                if self.pending_pings.remove(&causation_id).is_none() {
                    return Err(RuntimeWebSocketError::Contract {
                        reason: "pong causation is unknown",
                    });
                }
                self.last_valid_pong = Instant::now();
            }
            RuntimeServerMessage::RuntimeError {
                causation_id,
                payload,
                ..
            } => {
                self.handle_domain_error(causation_id, payload)?;
            }
        }
        Ok(())
    }

    fn complete_pty_input(
        &mut self,
        causation_id: Uuid,
        payload: PtyInputResultPayload,
    ) -> Result<(), RuntimeWebSocketError> {
        let pending =
            self.pending_inputs
                .remove(&causation_id)
                .ok_or(RuntimeWebSocketError::Contract {
                    reason: "PTY result causation is unknown",
                })?;
        if payload.pty_session_id != pending.pty_session_id
            || payload.accepted_bytes > MAX_PTY_BYTES as u32
            || (payload.error_code.is_none() && payload.accepted_bytes != pending.byte_count)
            || (payload.error_code.is_some() && payload.accepted_bytes != 0)
        {
            return Err(RuntimeWebSocketError::Contract {
                reason: "PTY result does not match its request",
            });
        }
        let result = match payload.error_code {
            Some(error_code) => Err(RuntimeClientError::WebSocketDomain { error_code }),
            None => Ok(PtyInputResult {
                pty_session_id: payload.pty_session_id,
                accepted_bytes: payload.accepted_bytes,
            }),
        };
        let _ = pending.reply.send(result);
        Ok(())
    }

    fn handle_domain_error(
        &mut self,
        causation_id: Option<Uuid>,
        payload: RuntimeErrorPayload,
    ) -> Result<(), RuntimeWebSocketError> {
        if let Some(causation_id) = causation_id {
            if let Some(pending) = self.pending_inputs.remove(&causation_id) {
                let _ = pending.reply.send(Err(RuntimeClientError::WebSocketDomain {
                    error_code: payload.error_code,
                }));
                return Ok(());
            }
        }
        Err(RuntimeWebSocketError::Domain {
            error_code: payload.error_code,
        })
    }

    async fn project_pty_output(&self, payload: PtyOutputPayload) {
        self.project(
            "ssh://event",
            json!({
                "event": "ssh.pty.output",
                "pty_session_id": payload.pty_session_id,
                "stream_sequence": payload.stream_sequence,
                "data_b64": payload.data_b64,
            }),
        )
        .await;
    }

    async fn project_pty_closed(&self, payload: PtyClosedPayload) {
        self.project(
            "ssh://event",
            json!({
                "event": "ssh.pty.closed",
                "pty_session_id": payload.pty_session_id,
                "exit_status": payload.exit_status,
                "exit_signal": payload.exit_signal,
            }),
        )
        .await;
    }

    async fn project_ssh(&self, payload: ConnectionStatus) {
        self.project(
            "ssh://event",
            json!({"event": "ssh.connection.status", "status": payload}),
        )
        .await;
    }

    async fn project(&self, event: &'static str, payload: Value) {
        if self
            .projection_tx
            .send(RuntimeProjection { event, payload })
            .await
            .is_err()
        {
            log::warn!(
                target: "harness_shell::runtime",
                "runtime event projection delivery failed: event={event}"
            );
        }
    }

    fn fail_pending(&mut self, error: RuntimeClientError) {
        for (_, pending) in self.pending_inputs.drain() {
            let _ = pending.reply.send(Err(error.clone()));
        }
    }
}

fn decode_text(message: Message) -> Result<RuntimeServerMessage, RuntimeWebSocketError> {
    let text = match message {
        Message::Text(text) => text,
        Message::Close(frame) => {
            return Err(match frame.map(|value| u16::from(value.code)) {
                Some(4409) => RuntimeWebSocketError::OwnerConflict,
                Some(4403) => RuntimeWebSocketError::RuntimeNotReady,
                Some(4408) => RuntimeWebSocketError::HeartbeatTimeout,
                Some(4400) => RuntimeWebSocketError::Contract {
                    reason: "server rejected the runtime WebSocket contract",
                },
                Some(1009) => RuntimeWebSocketError::MessageTooLarge,
                _ => RuntimeWebSocketError::Disconnected,
            })
        }
        Message::Ping(_) | Message::Pong(_) => {
            return Err(RuntimeWebSocketError::Contract {
                reason: "WebSocket control frame cannot carry runtime data",
            })
        }
        Message::Binary(_) | Message::Frame(_) => {
            return Err(RuntimeWebSocketError::Contract {
                reason: "runtime WebSocket requires text messages",
            })
        }
    };
    if text.len() > MAX_WEBSOCKET_TEXT_BYTES {
        return Err(RuntimeWebSocketError::MessageTooLarge);
    }
    serde_json::from_str(text.as_str()).map_err(|_| RuntimeWebSocketError::Contract {
        reason: "runtime WebSocket message is invalid",
    })
}

fn validate_base64(value: &str) -> Result<(), RuntimeWebSocketError> {
    let decoded = STANDARD
        .decode(value)
        .map_err(|_| RuntimeWebSocketError::Contract {
            reason: "PTY payload base64 is invalid",
        })?;
    if !(1..=MAX_PTY_BYTES).contains(&decoded.len()) || STANDARD.encode(decoded) != value {
        return Err(RuntimeWebSocketError::Contract {
            reason: "PTY payload base64 is not canonical",
        });
    }
    Ok(())
}

async fn send_json(
    socket: &mut RuntimeSocket,
    message: &RuntimeClientMessage,
) -> Result<(), RuntimeWebSocketError> {
    let encoded = serde_json::to_string(message).map_err(|_| RuntimeWebSocketError::Contract {
        reason: "runtime WebSocket message serialization failed",
    })?;
    if encoded.len() > MAX_WEBSOCKET_TEXT_BYTES {
        return Err(RuntimeWebSocketError::MessageTooLarge);
    }
    socket
        .send(Message::Text(encoded.into()))
        .await
        .map_err(|_| RuntimeWebSocketError::Disconnected)
}

#[derive(Serialize)]
#[serde(tag = "type")]
enum RuntimeClientMessage {
    #[serde(rename = "pty.input")]
    PtyInput {
        schema_version: u8,
        message_id: Uuid,
        causation_id: (),
        #[serde(with = "time::serde::rfc3339")]
        timestamp: time::OffsetDateTime,
        payload: PtyInputPayload,
    },
    #[serde(rename = "runtime.ping")]
    RuntimePing {
        schema_version: u8,
        message_id: Uuid,
        causation_id: (),
        #[serde(with = "time::serde::rfc3339")]
        timestamp: time::OffsetDateTime,
        payload: RuntimePingPayload,
    },
}

#[derive(Serialize)]
struct PtyInputPayload {
    pty_session_id: Uuid,
    data_b64: String,
}

#[derive(Serialize)]
struct RuntimePingPayload {
    #[serde(with = "time::serde::rfc3339")]
    client_timestamp: time::OffsetDateTime,
}

#[derive(Debug, thiserror::Error)]
pub enum RuntimeWebSocketError {
    #[error("runtime WebSocket configuration is invalid")]
    Configuration,
    #[error("runtime WebSocket connection failed")]
    Connect,
    #[error("runtime WebSocket disconnected")]
    Disconnected,
    #[error("runtime WebSocket already has an owner")]
    OwnerConflict,
    #[error("runtime is not ready for WebSocket ownership")]
    RuntimeNotReady,
    #[error("runtime WebSocket message exceeds the fixed limit")]
    MessageTooLarge,
    #[error("runtime WebSocket contract failed: {reason}")]
    Contract { reason: &'static str },
    #[error("runtime PTY output sequence has a gap")]
    SequenceGap,
    #[error("runtime WebSocket heartbeat timed out")]
    HeartbeatTimeout,
    #[error("runtime WebSocket domain failure")]
    Domain { error_code: String },
}

impl RuntimeWebSocketError {
    pub const fn error_code(&self) -> &'static str {
        match self {
            Self::Configuration => "RUNTIME_WEBSOCKET_CONFIGURATION_INVALID",
            Self::Connect => "RUNTIME_WEBSOCKET_CONNECT_FAILED",
            Self::Disconnected => "RUNTIME_WEBSOCKET_DISCONNECTED",
            Self::OwnerConflict => "RUNTIME_WEBSOCKET_OWNER_CONFLICT",
            Self::RuntimeNotReady => "RUNTIME_NOT_READY",
            Self::MessageTooLarge => "RUNTIME_WEBSOCKET_MESSAGE_TOO_LARGE",
            Self::Contract { .. } | Self::SequenceGap => "RUNTIME_WEBSOCKET_CONTRACT_FAILED",
            Self::HeartbeatTimeout => "RUNTIME_WEBSOCKET_HEARTBEAT_TIMEOUT",
            Self::Domain { .. } => "RUNTIME_WEBSOCKET_DOMAIN_FAILED",
        }
    }
}
