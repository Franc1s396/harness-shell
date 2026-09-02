use std::{fs, path::PathBuf};

use harness_shell_lib::runtime::models::{
    ProblemDetails, RuntimeInitializeBody, RuntimePhase, RuntimeStateResponse, JSON_BODY_MAX_BYTES,
};
use serde_json::{json, Value};
use uuid::Uuid;

fn http_fixture(name: &str) -> Vec<u8> {
    fs::read(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../docs/protocol/http/fixtures")
            .join(name),
    )
    .expect("read HTTP contract fixture")
}

#[test]
fn current_problem_fixture_is_the_strict_python_problem_shape() {
    let problem: ProblemDetails = serde_json::from_slice(&http_fixture("problem-details-v1.json"))
        .expect("current Problem Details fixture must decode");

    assert_eq!(problem.status, 429);
    assert_eq!(problem.error_code, "REQUEST_CAPACITY_EXCEEDED");
    assert_eq!(
        problem.request_id,
        Uuid::parse_str("2e8c0760-5959-4842-b4a5-71a11f6e0c04").unwrap()
    );
    assert_eq!(problem.details, serde_json::Map::new());
}

#[test]
fn strict_models_reject_unknown_fields_and_unknown_runtime_phase() {
    let request_id = Uuid::new_v4();
    let response = json!({
        "request_id": request_id,
        "state": "READY",
        "unexpected": true,
    });
    assert!(serde_json::from_value::<RuntimeStateResponse>(response).is_err());

    let response = json!({
        "request_id": request_id,
        "state": "RECONNECTING",
    });
    assert!(serde_json::from_value::<RuntimeStateResponse>(response).is_err());
}

#[test]
fn problem_details_reject_unknown_fields() {
    let value = json!({
        "type": "urn:harness-shell:error:connection-profile-changed",
        "title": "Connection profile changed",
        "status": 409,
        "error_code": "CONNECTION_PROFILE_CHANGED",
        "message": "Connection profile changed before network I/O",
        "request_id": "4b388231-432b-47c0-9522-158d791b4b43",
        "details": {},
        "unexpected": true,
    });

    assert!(serde_json::from_value::<ProblemDetails>(value).is_err());
}

#[test]
fn initialize_body_validates_absolute_path_keys_and_fixed_heartbeat() {
    let key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    RuntimeInitializeBody::new(
        "0.1.0",
        r"C:\Users\Example\runtime.sqlite3",
        key,
        key,
        5_000,
        15_000,
    )
    .expect("current initialize body must be valid");

    assert!(
        RuntimeInitializeBody::new("0.1.0", "relative.sqlite3", key, key, 5_000, 15_000,).is_err()
    );
    assert!(RuntimeInitializeBody::new(
        "0.1.0",
        r"C:\runtime.sqlite3",
        "c2hvcnQ=",
        key,
        5_000,
        15_000,
    )
    .is_err());
    assert!(
        RuntimeInitializeBody::new("0.1.0", r"C:\runtime.sqlite3", key, key, 4_999, 15_000,)
            .is_err()
    );
}

#[test]
fn initialize_debug_never_formats_runtime_keys() {
    let marker = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    let body = RuntimeInitializeBody::new(
        "0.1.0",
        r"C:\runtime.sqlite3",
        marker,
        marker,
        5_000,
        15_000,
    )
    .unwrap();

    let rendered = format!("{body:?}");
    assert!(!rendered.contains(marker));
    assert!(rendered.contains("<redacted>"));
}

#[test]
fn limits_fixture_matches_the_rust_json_boundary() {
    let limits: Value = serde_json::from_slice(&http_fixture("limits-v1.json")).unwrap();
    assert_eq!(limits["json_request_bytes"], JSON_BODY_MAX_BYTES);
    assert_eq!(limits["json_response_bytes"], JSON_BODY_MAX_BYTES);
    assert_eq!(RuntimePhase::Ready.as_str(), "READY");
}
