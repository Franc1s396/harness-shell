use std::time::Duration;

use super::status::{RuntimeState, RuntimeStatus, RuntimeSupervisorPhase};

const SHUTDOWN_GRACE: Duration = Duration::from_secs(3);

#[allow(async_fn_in_trait)]
pub trait RuntimeBackend {
    async fn start_process(&mut self) -> Result<(), RuntimeBackendError>;
    async fn wait_http_live(&mut self) -> Result<(), RuntimeBackendError>;
    async fn initialize(&mut self) -> Result<(), RuntimeBackendError>;
    async fn wait_http_ready(&mut self) -> Result<(), RuntimeBackendError>;
    async fn connect_websocket(&mut self) -> Result<(), RuntimeBackendError>;
    async fn request_shutdown(&mut self) -> Result<(), RuntimeBackendError>;
    async fn wait_for_exit(&mut self, grace: Duration) -> Result<bool, RuntimeBackendError>;
    fn kill_process_tree(&mut self) -> Result<(), RuntimeBackendError>;
}

#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
#[error("runtime backend stage failed")]
pub struct RuntimeBackendError {
    error_code: String,
}

impl RuntimeBackendError {
    pub fn new(error_code: impl Into<String>) -> Self {
        Self {
            error_code: error_code.into(),
        }
    }

    pub fn error_code(&self) -> &str {
        &self.error_code
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeFailure {
    ChildExited,
    WebSocketDisconnected,
    WebSocketContract,
    HeartbeatTimeout,
}

impl RuntimeFailure {
    const fn error_code(self) -> &'static str {
        match self {
            Self::ChildExited => "SIDECAR_EXITED",
            Self::WebSocketDisconnected => "RUNTIME_WEBSOCKET_DISCONNECTED",
            Self::WebSocketContract => "RUNTIME_WEBSOCKET_CONTRACT_FAILED",
            Self::HeartbeatTimeout => "RUNTIME_WEBSOCKET_HEARTBEAT_TIMEOUT",
        }
    }
}

pub struct RuntimeSupervisor<B> {
    backend: B,
    status: RuntimeStatus,
    internal_transitions: Vec<RuntimeSupervisorPhase>,
}

impl<B: RuntimeBackend> RuntimeSupervisor<B> {
    pub async fn start(backend: B) -> Self {
        let mut supervisor = Self {
            backend,
            status: RuntimeStatus::starting("desktop"),
            internal_transitions: Vec::with_capacity(6),
        };

        supervisor.transition(RuntimeSupervisorPhase::StartingProcess);
        if let Err(error) = supervisor.backend.start_process().await {
            supervisor.fail_once(error.error_code());
            return supervisor;
        }

        if let Err(error) = supervisor.start_after_process().await {
            // A successfully spawned process always remains owned by Rust. Cleanup
            // failure must not replace the first startup failure exposed to callers.
            let _ = supervisor.backend.kill_process_tree();
            supervisor.fail_once(error.error_code());
        }
        supervisor
    }

    async fn start_after_process(&mut self) -> Result<(), RuntimeBackendError> {
        self.backend.wait_http_live().await?;
        self.transition(RuntimeSupervisorPhase::HttpLive);
        self.transition(RuntimeSupervisorPhase::Initializing);
        self.backend.initialize().await?;
        self.backend.wait_http_ready().await?;
        self.transition(RuntimeSupervisorPhase::HttpReady);
        self.backend.connect_websocket().await?;
        self.transition(RuntimeSupervisorPhase::WebSocketConnected);
        self.transition(RuntimeSupervisorPhase::Ready);
        self.status.state = RuntimeState::Ready;
        Ok(())
    }

    pub fn public_status(&self) -> &RuntimeStatus {
        &self.status
    }

    pub fn internal_transitions(&self) -> &[RuntimeSupervisorPhase] {
        &self.internal_transitions
    }

    pub fn backend(&self) -> &B {
        &self.backend
    }

    pub fn fail_runtime(&mut self, failure: RuntimeFailure) {
        self.fail_once(failure.error_code());
    }

    pub async fn shutdown(&mut self) -> Result<(), RuntimeBackendError> {
        let request_error = self.backend.request_shutdown().await.err();
        let exited = self.backend.wait_for_exit(SHUTDOWN_GRACE).await?;
        if !exited {
            self.backend.kill_process_tree()?;
        }
        if let Some(error) = request_error {
            self.fail_once(error.error_code());
            return Err(error);
        }
        self.status.state = RuntimeState::Stopped;
        self.status.error_code = None;
        self.status.recoverable = false;
        Ok(())
    }

    fn transition(&mut self, phase: RuntimeSupervisorPhase) {
        self.internal_transitions.push(phase);
        if matches!(
            phase,
            RuntimeSupervisorPhase::HttpLive
                | RuntimeSupervisorPhase::Initializing
                | RuntimeSupervisorPhase::HttpReady
                | RuntimeSupervisorPhase::WebSocketConnected
        ) {
            self.status.state = RuntimeState::Handshaking;
        }
    }

    fn fail_once(&mut self, error_code: &str) {
        if self.status.state == RuntimeState::Failed && self.status.error_code.is_some() {
            return;
        }
        self.status.state = RuntimeState::Failed;
        self.status.error_code = Some(error_code.to_owned());
        self.status.recoverable = false;
    }
}
