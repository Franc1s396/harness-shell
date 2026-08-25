use harness_shell_lib::sidecar::{
    RuntimeState, RuntimeStatus, Supervisor, SupervisorAction, SupervisorEvent,
    SupervisorTransition,
};
use time::OffsetDateTime;
use uuid::Uuid;

fn status(state: RuntimeState) -> RuntimeStatus {
    RuntimeStatus {
        state,
        error_code: None,
        node: "desktop".into(),
        recoverable: false,
        correlation_id: Uuid::nil(),
        last_sequence: 0,
        last_heartbeat_at: None,
    }
}

fn transition_from(state: RuntimeState, event: SupervisorEvent) -> SupervisorTransition {
    let mut supervisor = Supervisor::new(status(state));
    supervisor.transition(event)
}

fn assert_transition(
    from: RuntimeState,
    event: SupervisorEvent,
    to: RuntimeState,
    error_code: Option<&str>,
) {
    let transition = transition_from(from, event);
    assert_eq!(transition.status.state, to);
    assert_eq!(transition.status.error_code.as_deref(), error_code);
}

#[test]
fn supervisor_reaches_ready_only_after_spawn_and_initialize() {
    assert_transition(
        RuntimeState::Starting,
        SupervisorEvent::Spawned,
        RuntimeState::Handshaking,
        None,
    );
    assert_transition(
        RuntimeState::Handshaking,
        SupervisorEvent::InitializeAccepted,
        RuntimeState::Ready,
        None,
    );
}

#[test]
fn runtime_failures_pause_without_automatic_respawn() {
    assert_transition(
        RuntimeState::Ready,
        SupervisorEvent::HeartbeatTimedOut,
        RuntimeState::Paused,
        Some("SIDECAR_HEARTBEAT_TIMEOUT"),
    );
    assert_transition(
        RuntimeState::Ready,
        SupervisorEvent::SequenceGap {
            expected: 7,
            actual: 9,
        },
        RuntimeState::Paused,
        Some("SIDECAR_SEQUENCE_GAP"),
    );

    let transition = transition_from(
        RuntimeState::Ready,
        SupervisorEvent::ProcessExited { code: Some(1) },
    );
    assert_eq!(transition.status.state, RuntimeState::Paused);
    assert_eq!(
        transition.status.error_code.as_deref(),
        Some("SIDECAR_EXITED")
    );
    assert!(!transition.actions.contains(&SupervisorAction::Spawn));
}

#[test]
fn protocol_failures_are_terminal_and_never_respawn() {
    for event in [
        SupervisorEvent::ProtocolVersionMismatch { actual: 2 },
        SupervisorEvent::InvalidFrame,
        SupervisorEvent::InvalidInitializeResponse,
    ] {
        let transition = transition_from(RuntimeState::Handshaking, event);
        assert_eq!(transition.status.state, RuntimeState::Failed);
        assert!(!transition.status.recoverable);
        assert!(!transition.actions.contains(&SupervisorAction::Spawn));
    }
}

#[test]
fn first_fatal_cause_survives_process_teardown() {
    let mut supervisor = Supervisor::new(status(RuntimeState::Handshaking));
    let first = supervisor.transition(SupervisorEvent::ProtocolVersionMismatch { actual: 2 });
    assert_eq!(
        first.status.error_code.as_deref(),
        Some("SIDECAR_PROTOCOL_VERSION_UNSUPPORTED")
    );

    let after_exit = supervisor.transition(SupervisorEvent::ProcessExited { code: Some(1) });
    assert_eq!(after_exit.status, first.status);
    assert!(after_exit.actions.is_empty());
}

#[test]
fn heartbeat_updates_sequence_and_timestamp() {
    let at = OffsetDateTime::UNIX_EPOCH;
    let transition = transition_from(
        RuntimeState::Ready,
        SupervisorEvent::HeartbeatReceived { sequence: 7, at },
    );

    assert_eq!(transition.status.last_sequence, 7);
    assert_eq!(transition.status.last_heartbeat_at, Some(at));
    assert_eq!(transition.status.state, RuntimeState::Ready);
}

#[test]
fn shutdown_timeout_fails_without_respawn() {
    let transition = transition_from(RuntimeState::Ready, SupervisorEvent::ShutdownTimedOut);

    assert_eq!(transition.status.state, RuntimeState::Failed);
    assert_eq!(
        transition.status.error_code.as_deref(),
        Some("SIDECAR_SHUTDOWN_TIMEOUT")
    );
    assert!(!transition.actions.contains(&SupervisorAction::Spawn));
}
