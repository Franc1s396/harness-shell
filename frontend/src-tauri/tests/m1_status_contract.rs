use harness_shell_lib::runtime::{RuntimeState, RuntimeStatus};
use serde_json::json;
use time::OffsetDateTime;
use uuid::Uuid;

fn status(state: RuntimeState) -> RuntimeStatus {
    RuntimeStatus {
        state,
        error_code: None,
        node: "desktop".into(),
        recoverable: false,
        correlation_id: Uuid::new_v4(),
        last_heartbeat_at: Some(OffsetDateTime::now_utc()),
    }
}

#[test]
fn public_runtime_status_preserves_the_existing_ui_state_vocabulary() {
    let states = [
        RuntimeState::Starting,
        RuntimeState::Handshaking,
        RuntimeState::Ready,
        RuntimeState::Paused,
        RuntimeState::Failed,
        RuntimeState::Stopped,
    ];
    let encoded = states
        .into_iter()
        .map(|state| serde_json::to_value(state).unwrap())
        .collect::<Vec<_>>();

    assert_eq!(
        serde_json::Value::Array(encoded),
        json!([
            "STARTING",
            "HANDSHAKING",
            "READY",
            "PAUSED",
            "FAILED",
            "STOPPED"
        ])
    );
}

#[test]
fn public_runtime_status_exposes_no_transport_global_sequence() {
    let encoded = serde_json::to_value(status(RuntimeState::Ready)).unwrap();

    assert!(encoded.get("last_sequence").is_none());
    assert!(encoded.get("last_heartbeat_at").is_some());
    assert_eq!(encoded["node"], "desktop");
}
