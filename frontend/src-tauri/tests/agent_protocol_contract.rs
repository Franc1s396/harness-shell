use std::{collections::BTreeSet, fs, path::PathBuf};

use serde_json::Value;

fn fixture_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../docs/protocol/fixtures/agent")
}

fn json_fixture(name: &str) -> Value {
    serde_json::from_slice(&fs::read(fixture_root().join(name)).expect("read Agent fixture"))
        .expect("parse Agent fixture")
}

#[test]
fn shared_fixture_covers_exact_agent_methods_and_sensitivity() {
    let valid = json_fixture("valid-method-pairs-v1.json");
    let methods = valid
        .as_array()
        .expect("valid Agent pairs must be an array")
        .iter()
        .map(|pair| {
            (
                pair["method"].as_str().expect("method"),
                pair["sensitivity"].as_str().expect("sensitivity"),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        methods,
        vec![
            ("agent.api_configs.list", "normal"),
            ("agent.api_configs.get", "normal"),
            ("agent.api_configs.create", "normal"),
            ("agent.api_configs.update", "normal"),
            ("agent.api_configs.delete", "normal"),
            ("agent.turn.run", "secret"),
        ]
    );
}

#[test]
fn shared_invalid_fixture_freezes_every_agent_boundary() {
    let invalid = json_fixture("invalid-cases-v1.json");
    let kinds = invalid
        .as_array()
        .expect("invalid Agent cases must be an array")
        .iter()
        .map(|case| case["kind"].as_str().expect("kind"))
        .collect::<BTreeSet<_>>();
    assert_eq!(
        kinds,
        BTreeSet::from([
            "normal_frame_api_key",
            "unknown_request_field",
            "malformed_uuid",
            "malformed_base64",
            "tool_session_argument_forbidden",
            "oversized_agent_response",
            "unmatched_tool_call_repair",
        ])
    );
    assert!(invalid[5]["encoded_payload_bytes"].as_u64().unwrap() > 1_048_576);
    assert_eq!(
        invalid[6]["repair"]["code"],
        "PREVIOUS_TOOL_CALL_INTERRUPTED"
    );
}

#[test]
fn shared_fixture_freezes_public_agent_error_codes() {
    let public = json_fixture("public-error-codes-v1.json");
    let turn = public["turn"]
        .as_array()
        .expect("turn error codes must be an array");
    for required in [
        "MODEL_API_CONFIG_NOT_FOUND",
        "MODEL_API_CONFIG_DISABLED",
        "MODEL_REQUEST_FAILED",
        "MODEL_RESPONSE_INVALID",
        "AGENT_CONVERSATION_NOT_FOUND",
        "AGENT_TURN_FAILED",
    ] {
        let category = if required.starts_with("MODEL_API_CONFIG_") {
            &public["api_config"]
        } else {
            &public["turn"]
        };
        assert!(
            category.as_array().unwrap().iter().any(|code| code == required),
            "public Agent error fixture is missing {required}"
        );
    }
    assert!(!turn.iter().any(|code| code == "AGENT_RUN_NOT_RUNNING"));
}

#[test]
fn protocol_reference_lists_every_agent_method_and_stable_error() {
    let protocol = fs::read_to_string(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../docs/protocol/v1.md"),
    )
    .expect("read Protocol v1 reference");
    for required in [
        "agent.api_configs.list",
        "agent.api_configs.get",
        "agent.api_configs.create",
        "agent.api_configs.update",
        "agent.api_configs.delete",
        "agent.turn.run",
        "AGENT_NORMAL_FRAME_REQUIRED",
        "AGENT_SECRET_FRAME_REQUIRED",
        "MODEL_API_CONFIG_NOT_FOUND",
        "MODEL_API_CONFIG_DISABLED",
        "MODEL_API_CONFIG_IN_USE",
        "MODEL_API_CONFIG_CHANGED",
        "COMMAND_REJECTED_INVALID_ARGUMENTS",
        "COMMAND_REJECTED_DANGEROUS_PATTERN",
        "MULTIPLE_TOOL_CALLS_UNSUPPORTED",
        "SSH_SESSION_UNAVAILABLE",
        "SSH_SESSION_LOST",
        "COMMAND_TIMEOUT",
        "COMMAND_OUTPUT_INVALID_UTF8",
        "COMMAND_EXECUTION_ERROR",
        "PREVIOUS_TOOL_CALL_INTERRUPTED",
        "REACT_LIMIT_REACHED",
        "MODEL_NETWORK_TIMEOUT",
        "MODEL_REQUEST_FAILED",
        "MODEL_RESPONSE_INVALID",
        "AGENT_CONVERSATION_NOT_FOUND",
        "AGENT_TURN_FAILED",
        "SIDECAR_RUNTIME_FAILED",
        "AGENT_CANCELLED",
        "AGENT_RESPONSE_TOO_LARGE",
    ] {
        assert!(protocol.contains(required), "Protocol v1 is missing {required}");
    }
}
