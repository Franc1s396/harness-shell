use time::OffsetDateTime;

use super::status::{RuntimeState, RuntimeStatus};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SupervisorEvent {
    StartRequested,
    Spawned,
    SpawnFailed,
    InitializeAccepted,
    InitializeRejected { error_code: String },
    InvalidInitializeResponse,
    ProtocolVersionMismatch { actual: u64 },
    InvalidFrame,
    HeartbeatDue,
    HeartbeatReceived { sequence: u64, at: OffsetDateTime },
    HeartbeatTimedOut,
    SequenceGap { expected: u64, actual: u64 },
    ShutdownRequested,
    ShutdownTimedOut,
    ShutdownCompleted,
    ProcessExited { code: Option<i32> },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SupervisorAction {
    Spawn,
    SendInitialize,
    SendHeartbeat,
    SendShutdown,
    PublishStatus,
    KillAfterGrace,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SupervisorTransition {
    pub status: RuntimeStatus,
    pub actions: Vec<SupervisorAction>,
}

pub struct Supervisor {
    status: RuntimeStatus,
    shutdown_requested: bool,
}

impl Supervisor {
    pub fn new(status: RuntimeStatus) -> Self {
        Self {
            status,
            shutdown_requested: false,
        }
    }

    pub fn status(&self) -> &RuntimeStatus {
        &self.status
    }

    pub fn transition(&mut self, event: SupervisorEvent) -> SupervisorTransition {
        if self.has_terminal_error() || self.status.state == RuntimeState::Stopped {
            return self.result(Vec::new());
        }

        let actions = match event {
            SupervisorEvent::StartRequested if self.status.state == RuntimeState::Starting => {
                vec![SupervisorAction::Spawn]
            }
            SupervisorEvent::Spawned if self.status.state == RuntimeState::Starting => {
                self.enter(RuntimeState::Handshaking, None, false);
                vec![
                    SupervisorAction::SendInitialize,
                    SupervisorAction::PublishStatus,
                ]
            }
            SupervisorEvent::InitializeAccepted
                if self.status.state == RuntimeState::Handshaking =>
            {
                self.enter(RuntimeState::Ready, None, false);
                vec![SupervisorAction::PublishStatus]
            }
            SupervisorEvent::HeartbeatDue if self.status.state == RuntimeState::Ready => {
                vec![SupervisorAction::SendHeartbeat]
            }
            SupervisorEvent::HeartbeatReceived { sequence, at }
                if self.status.state == RuntimeState::Ready =>
            {
                self.status.last_sequence = sequence;
                self.status.last_heartbeat_at = Some(at);
                vec![SupervisorAction::PublishStatus]
            }
            SupervisorEvent::HeartbeatTimedOut if self.status.state == RuntimeState::Ready => {
                self.pause("SIDECAR_HEARTBEAT_TIMEOUT", true)
            }
            SupervisorEvent::SequenceGap { .. }
                if matches!(
                    self.status.state,
                    RuntimeState::Handshaking | RuntimeState::Ready
                ) =>
            {
                self.pause("SIDECAR_SEQUENCE_GAP", false)
            }
            SupervisorEvent::ProtocolVersionMismatch { .. } => {
                self.fail("SIDECAR_PROTOCOL_VERSION_UNSUPPORTED")
            }
            SupervisorEvent::InvalidFrame => self.fail("SIDECAR_PROTOCOL_VIOLATION"),
            SupervisorEvent::InvalidInitializeResponse => self.fail("SIDECAR_INITIALIZE_INVALID"),
            SupervisorEvent::InitializeRejected { error_code } => self.fail(&error_code),
            SupervisorEvent::SpawnFailed => self.fail("SIDECAR_SPAWN_FAILED"),
            SupervisorEvent::ShutdownRequested => {
                self.shutdown_requested = true;
                vec![
                    SupervisorAction::SendShutdown,
                    SupervisorAction::KillAfterGrace,
                ]
            }
            SupervisorEvent::ShutdownTimedOut => self.fail("SIDECAR_SHUTDOWN_TIMEOUT"),
            SupervisorEvent::ShutdownCompleted => {
                self.enter(RuntimeState::Stopped, None, false);
                vec![SupervisorAction::PublishStatus]
            }
            SupervisorEvent::ProcessExited { .. } if self.shutdown_requested => {
                self.enter(RuntimeState::Stopped, None, false);
                vec![SupervisorAction::PublishStatus]
            }
            SupervisorEvent::ProcessExited { .. } => self.pause("SIDECAR_EXITED", true),
            _ => self.fail("SIDECAR_INVALID_STATE_TRANSITION"),
        };

        self.result(actions)
    }

    fn has_terminal_error(&self) -> bool {
        matches!(
            self.status.state,
            RuntimeState::Paused | RuntimeState::Failed
        ) && self.status.error_code.is_some()
    }

    fn pause(&mut self, error_code: &str, recoverable: bool) -> Vec<SupervisorAction> {
        self.enter(RuntimeState::Paused, Some(error_code), recoverable);
        vec![SupervisorAction::PublishStatus]
    }

    fn fail(&mut self, error_code: &str) -> Vec<SupervisorAction> {
        self.enter(RuntimeState::Failed, Some(error_code), false);
        vec![SupervisorAction::PublishStatus]
    }

    fn enter(&mut self, state: RuntimeState, error_code: Option<&str>, recoverable: bool) {
        self.status.state = state;
        self.status.error_code = error_code.map(str::to_owned);
        self.status.recoverable = recoverable;
    }

    fn result(&self, actions: Vec<SupervisorAction>) -> SupervisorTransition {
        SupervisorTransition {
            status: self.status.clone(),
            actions,
        }
    }
}
