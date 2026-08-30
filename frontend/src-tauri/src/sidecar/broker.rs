use std::{collections::HashMap, fmt};

use serde_json::{Map, Value};
use tokio::sync::{mpsc, oneshot};
use uuid::Uuid;

use crate::protocol::{FrameEnvelope, Sensitivity};
use crate::sftp::{events::parse_manual_sftp_progress, models::MutationProgressProjection};

pub const BROKER_CAPACITY: usize = 32;

#[derive(Clone)]
pub struct RuntimeBrokerHandle {
    sender: mpsc::Sender<RuntimeCommand>,
}

#[derive(Clone)]
pub struct RuntimeRequest {
    pub sensitivity: Sensitivity,
    pub payload: Map<String, Value>,
    pub task_id: Option<Uuid>,
    pub workflow_run_id: Option<Uuid>,
}

impl RuntimeRequest {
    pub fn normal(payload: Map<String, Value>) -> Self {
        Self {
            sensitivity: Sensitivity::Normal,
            payload,
            task_id: None,
            workflow_run_id: None,
        }
    }

    pub fn secret(payload: Map<String, Value>) -> Self {
        Self {
            sensitivity: Sensitivity::Secret,
            payload,
            task_id: None,
            workflow_run_id: None,
        }
    }
}

impl fmt::Debug for RuntimeRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RuntimeRequest")
            .field("sensitivity", &self.sensitivity)
            .field("payload", &"<redacted>")
            .field("task_id", &self.task_id)
            .field("workflow_run_id", &self.workflow_run_id)
            .finish()
    }
}

pub enum RuntimeCommand {
    Request {
        request_id: Uuid,
        request: RuntimeRequest,
        reply: oneshot::Sender<Result<FrameEnvelope, BrokerError>>,
    },
    Cancel {
        target_request_id: Uuid,
        reply: oneshot::Sender<Result<FrameEnvelope, BrokerError>>,
    },
    Shutdown,
}

pub struct PendingReplies {
    replies: HashMap<Uuid, oneshot::Sender<Result<FrameEnvelope, BrokerError>>>,
}

impl PendingReplies {
    pub fn new() -> Self {
        Self {
            replies: HashMap::new(),
        }
    }

    pub fn insert(
        &mut self,
        request_id: Uuid,
        reply: oneshot::Sender<Result<FrameEnvelope, BrokerError>>,
    ) -> Result<(), BrokerError> {
        match self.replies.entry(request_id) {
            std::collections::hash_map::Entry::Vacant(entry) => {
                entry.insert(reply);
                Ok(())
            }
            std::collections::hash_map::Entry::Occupied(_) => Err(BrokerError::Protocol),
        }
    }

    pub fn complete(&mut self, frame: FrameEnvelope) -> Result<(), BrokerError> {
        if !matches!(
            frame.message_type,
            crate::protocol::MessageType::Response | crate::protocol::MessageType::Error
        ) {
            return Err(BrokerError::Protocol);
        }
        let reply = self
            .replies
            .remove(&frame.request_id)
            .ok_or(BrokerError::UnknownResponse)?;
        let _ = reply.send(Ok(frame));
        Ok(())
    }

    pub fn contains(&self, request_id: Uuid) -> bool {
        self.replies.contains_key(&request_id)
    }

    pub fn fail_all(&mut self, error: BrokerError) {
        for (_, reply) in self.replies.drain() {
            let _ = reply.send(Err(error.clone()));
        }
    }
}

impl Default for PendingReplies {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
pub enum BrokerError {
    #[error("Sidecar broker is closed")]
    Closed,
    #[error("Sidecar runtime is not ready")]
    RuntimeNotReady,
    #[error("Sidecar response request ID is unknown")]
    UnknownResponse,
    #[error("Sidecar event is not allowlisted")]
    UnknownEvent,
    #[error("Sidecar broker protocol failed")]
    Protocol,
}

impl BrokerError {
    pub const fn error_code(&self) -> &'static str {
        match self {
            Self::Closed => "SIDECAR_BROKER_CLOSED",
            Self::RuntimeNotReady => "SIDECAR_NOT_READY",
            Self::UnknownResponse => "SIDECAR_UNKNOWN_RESPONSE",
            Self::UnknownEvent => "SIDECAR_UNKNOWN_EVENT",
            Self::Protocol => "SIDECAR_BROKER_PROTOCOL_FAILED",
        }
    }
}

impl RuntimeBrokerHandle {
    pub async fn request(&self, request: RuntimeRequest) -> Result<FrameEnvelope, BrokerError> {
        let (reply, receiver) = oneshot::channel();
        self.sender
            .send(RuntimeCommand::Request {
                request_id: Uuid::new_v4(),
                request,
                reply,
            })
            .await
            .map_err(|_| BrokerError::Closed)?;
        receiver.await.map_err(|_| BrokerError::Closed)?
    }

    pub async fn cancel(&self, target_request_id: Uuid) -> Result<FrameEnvelope, BrokerError> {
        let (reply, receiver) = oneshot::channel();
        self.sender
            .send(RuntimeCommand::Cancel {
                target_request_id,
                reply,
            })
            .await
            .map_err(|_| BrokerError::Closed)?;
        receiver.await.map_err(|_| BrokerError::Closed)?
    }

    pub fn request_shutdown(&self) {
        let _ = self.sender.try_send(RuntimeCommand::Shutdown);
    }
}

pub fn runtime_broker_channel() -> (RuntimeBrokerHandle, mpsc::Receiver<RuntimeCommand>) {
    let (sender, receiver) = mpsc::channel(BROKER_CAPACITY);
    (RuntimeBrokerHandle { sender }, receiver)
}

#[derive(Clone, Debug, PartialEq)]
pub enum RuntimeEventRoute {
    Ssh(Value),
    ManualSftpOperation(MutationProgressProjection),
}

#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeEventProjection {
    pub webview_event: &'static str,
    pub payload: Value,
}

pub fn project_runtime_event(
    payload: &Map<String, Value>,
) -> Result<RuntimeEventProjection, BrokerError> {
    let route = match payload.get("event").and_then(Value::as_str) {
        Some("ssh.connection.status" | "ssh.pty.output" | "ssh.pty.closed") => {
            RuntimeEventRoute::Ssh(Value::Object(payload.clone()))
        }
        Some("manual_sftp.operation.progress") => RuntimeEventRoute::ManualSftpOperation(
            parse_manual_sftp_progress(payload).map_err(|_| BrokerError::Protocol)?,
        ),
        _ => return Err(BrokerError::UnknownEvent),
    };

    match route {
        RuntimeEventRoute::Ssh(payload) => Ok(RuntimeEventProjection {
            webview_event: "ssh://event",
            payload,
        }),
        RuntimeEventRoute::ManualSftpOperation(progress) => Ok(RuntimeEventProjection {
            webview_event: "manual-sftp://operation-state",
            payload: serde_json::to_value(progress).map_err(|_| BrokerError::Protocol)?,
        }),
    }
}

/// Strictly project one allowlisted runtime event and treat WebView delivery as observation only.
pub fn emit_runtime_event_projection<E>(
    payload: &Map<String, Value>,
    emit: impl FnOnce(&RuntimeEventProjection) -> Result<(), E>,
) -> Result<(), BrokerError> {
    let projection = project_runtime_event(payload)?;
    if emit(&projection).is_err() {
        // Keep diagnostics bounded and payload-free: projection succeeded, so delivery failure
        // cannot change the Sidecar operation result or terminate the protocol owner.
        log::warn!(
            target: "harness_shell::sidecar",
            "runtime event projection delivery failed: event={}",
            projection.webview_event
        );
    }
    Ok(())
}

pub fn validate_runtime_event(payload: &Map<String, Value>) -> Result<(), BrokerError> {
    project_runtime_event(payload).map(|_| ())
}
