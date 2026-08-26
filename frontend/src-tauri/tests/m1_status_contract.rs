use harness_shell_lib::{
    protocol::{FrameEnvelope, MessageType, Sensitivity, PROTOCOL_VERSION},
    sidecar::{
        process::{
            advance_heartbeat_clock, apply_heartbeat_timeout, supervisor_event_for_process_error,
            validate_initialize_response, ProcessError,
        },
        RuntimeState, RuntimeStatus, Supervisor, SupervisorAction, SupervisorEvent,
    },
};
use serde_json::{json, Map};
use time::OffsetDateTime;
use tokio::time::Instant;
use uuid::Uuid;

fn status(state: RuntimeState) -> RuntimeStatus {
    RuntimeStatus {
        state,
        error_code: None,
        node: "desktop".into(),
        recoverable: false,
        correlation_id: Uuid::new_v4(),
        last_sequence: 0,
        last_heartbeat_at: None,
    }
}

fn error_frame(request_id: Uuid, error_code: &str) -> FrameEnvelope {
    FrameEnvelope {
        protocol_version: PROTOCOL_VERSION,
        message_type: MessageType::Error,
        request_id,
        task_id: None,
        workflow_run_id: None,
        sequence: 2,
        timestamp: OffsetDateTime::now_utc(),
        sensitivity: Sensitivity::Normal,
        payload: Map::from_iter([
            ("error_code".into(), json!(error_code)),
            ("message".into(), json!("public initialization failure")),
        ]),
    }
}

#[test]
fn startup_failure_keeps_the_sidecar_error_code_and_first_cause() {
    let request_id = Uuid::new_v4();
    let error =
        validate_initialize_response(&error_frame(request_id, "AUDIT_CHAIN_INVALID"), request_id)
            .expect_err("initialization must fail");
    assert!(matches!(
        error,
        ProcessError::InitializeRejected { ref error_code }
            if error_code == "AUDIT_CHAIN_INVALID"
    ));

    let mut supervisor = Supervisor::new(status(RuntimeState::Handshaking));
    let event =
        supervisor_event_for_process_error(&error, SupervisorEvent::InvalidInitializeResponse);
    let failed = supervisor.transition(event);
    assert_eq!(failed.status.state, RuntimeState::Failed);
    assert_eq!(
        failed.status.error_code.as_deref(),
        Some("AUDIT_CHAIN_INVALID")
    );
    let after_exit = supervisor.transition(SupervisorEvent::ProcessExited { code: Some(1) });
    assert_eq!(after_exit.status, failed.status);
}

#[test]
fn forged_or_unknown_initialize_error_codes_are_protocol_violations() {
    for error_code in ["SIDECAR_EXITED", "READY", "UNRECOGNIZED_INITIALIZE_ERROR"] {
        let request_id = Uuid::new_v4();
        assert!(matches!(
            validate_initialize_response(&error_frame(request_id, error_code), request_id),
            Err(ProcessError::InvalidFrame("initialize error"))
        ));
    }
}

#[test]
fn protocol_mismatch_fails_before_ready() {
    let error = ProcessError::Protocol(
        harness_shell_lib::protocol::ProtocolError::UnsupportedProtocolVersion { actual: 2 },
    );
    let mut supervisor = Supervisor::new(status(RuntimeState::Handshaking));
    let event = supervisor_event_for_process_error(&error, SupervisorEvent::InvalidFrame);
    let transition = supervisor.transition(event);
    assert_eq!(transition.status.state, RuntimeState::Failed);
    assert_eq!(
        transition.status.error_code.as_deref(),
        Some("SIDECAR_PROTOCOL_VERSION_UNSUPPORTED")
    );
}

#[test]
fn process_exit_after_ready_pauses_without_respawn() {
    let original = status(RuntimeState::Ready);
    let correlation_id = original.correlation_id;
    let mut supervisor = Supervisor::new(original);
    let transition = supervisor.transition(SupervisorEvent::ProcessExited { code: Some(1) });
    assert_eq!(transition.status.state, RuntimeState::Paused);
    assert!(transition.status.recoverable);
    assert_eq!(transition.status.correlation_id, correlation_id);
    assert!(!transition.actions.contains(&SupervisorAction::Spawn));
}

#[test]
fn sixteen_second_heartbeat_gap_pauses_runtime() {
    tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .expect("build paused-time runtime")
        .block_on(async {
            tokio::time::pause();
            let last_valid = Instant::now();
            tokio::time::advance(std::time::Duration::from_secs(16)).await;
            let mut supervisor = Supervisor::new(status(RuntimeState::Ready));
            let transition = apply_heartbeat_timeout(&mut supervisor, last_valid, Instant::now())
                .expect("production timeout transition");
            assert_eq!(transition.status.state, RuntimeState::Paused);
            assert_eq!(
                transition.status.error_code.as_deref(),
                Some("SIDECAR_HEARTBEAT_TIMEOUT")
            );
        });
}

#[test]
fn application_frames_do_not_refresh_the_heartbeat_clock() {
    let last_valid_pong = Instant::now();
    let application_frame_at = last_valid_pong + std::time::Duration::from_secs(14);

    assert_eq!(
        advance_heartbeat_clock(last_valid_pong, application_frame_at, false),
        last_valid_pong
    );
    assert_eq!(
        advance_heartbeat_clock(last_valid_pong, application_frame_at, true),
        application_frame_at
    );
}
