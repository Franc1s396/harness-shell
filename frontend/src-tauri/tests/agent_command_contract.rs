#![cfg(target_os = "windows")]

use std::sync::Arc;

use base64::{engine::general_purpose::STANDARD, Engine as _};
use harness_shell_lib::{
    commands::run_agent_turn_with_dependencies,
    protocol::{FrameEnvelope, MessageType, Sensitivity},
    sidecar::broker::{runtime_broker_channel, RuntimeCommand, RuntimeRequest},
    vault::{CredentialKind, SecretVault, VaultState},
};
use serde_json::{json, Map, Value};
use tempfile::tempdir;
use time::OffsetDateTime;
use uuid::Uuid;

fn object(value: Value) -> Map<String, Value> {
    value.as_object().expect("value must be an object").clone()
}

fn response(request_id: Uuid, payload: Value) -> FrameEnvelope {
    FrameEnvelope {
        protocol_version: 1,
        message_type: MessageType::Response,
        request_id,
        task_id: None,
        workflow_run_id: None,
        sequence: 1,
        timestamp: OffsetDateTime::now_utc(),
        sensitivity: Sensitivity::Normal,
        payload: object(payload),
    }
}

#[test]
fn agent_turn_reads_config_normally_then_sends_only_the_turn_as_secret() {
    tauri::async_runtime::block_on(async {
        let directory = tempdir().expect("create Vault directory");
        let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open Vault");
        let api_key_id = vault
            .put_secret(CredentialKind::ApiKey, b"model-key-marker")
            .expect("store API key");
        let vault = Arc::new(VaultState::new(vault));
        let (broker, mut commands) = runtime_broker_channel();
        let api_config_id = Uuid::new_v4();
        let ssh_session_id = Uuid::new_v4();

        let running = tokio::spawn({
            let broker = broker.clone();
            let vault = Arc::clone(&vault);
            async move {
                run_agent_turn_with_dependencies(
                    &broker,
                    &vault,
                    None,
                    ssh_session_id,
                    api_config_id,
                    "inspect the host".to_owned(),
                )
                .await
            }
        });

        let RuntimeCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.expect("config lookup request")
        else {
            panic!("expected config lookup request")
        };
        assert_eq!(request.sensitivity, Sensitivity::Normal);
        assert_eq!(request.payload["method"], "agent.api_configs.get");
        assert!(request.payload["params"].get("api_key_b64").is_none());
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "config": {
                        "api_config_id": api_config_id,
                        "display_name": "test provider",
                        "api_type": "RESPONSES",
                        "base_url": "https://provider.example/v1/",
                        "model": "test-model",
                        "api_key_secret_ref": api_key_id,
                        "enabled": true,
                        "created_at": "2026-08-30T00:00:00Z",
                        "updated_at": "2026-08-30T00:00:00Z"
                    }
                }),
            )))
            .expect("config request remains active");

        let RuntimeCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.expect("secret turn request")
        else {
            panic!("expected secret turn request")
        };
        assert_eq!(request.sensitivity, Sensitivity::Secret);
        assert_eq!(request.payload["method"], "agent.turn.run");
        assert_eq!(
            request.payload["params"]["api_key_b64"],
            STANDARD.encode(b"model-key-marker")
        );
        assert_eq!(
            request.payload["params"]["api_key_credential_id"],
            api_key_id.to_string()
        );
        assert_eq!(
            request.payload["params"]["ssh_session_id"],
            ssh_session_id.to_string()
        );
        assert!(format!("{request:?}").contains("<redacted>"));
        assert!(!format!("{request:?}").contains("model-key-marker"));
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "conversation_id": Uuid::new_v4(),
                    "agent_run_id": Uuid::new_v4(),
                    "status": "COMPLETED",
                    "final_text": "done",
                    "react_iteration": 0,
                    "error_code": null
                }),
            )))
            .expect("turn request remains active");

        let result = running.await.expect("join turn task").expect("run turn");
        assert_eq!(result.final_text.as_deref(), Some("done"));
    });
}

#[test]
fn non_api_credential_fails_before_agent_turn_request() {
    tauri::async_runtime::block_on(async {
        let directory = tempdir().expect("create Vault directory");
        let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open Vault");
        let wrong_id = vault
            .put_secret(CredentialKind::SshPassword, b"wrong-kind")
            .expect("store SSH password");
        let vault = Arc::new(VaultState::new(vault));
        let (broker, mut commands) = runtime_broker_channel();
        let api_config_id = Uuid::new_v4();

        let running = tokio::spawn({
            let broker = broker.clone();
            let vault = Arc::clone(&vault);
            async move {
                run_agent_turn_with_dependencies(
                    &broker,
                    &vault,
                    None,
                    Uuid::new_v4(),
                    api_config_id,
                    "inspect".to_owned(),
                )
                .await
            }
        });
        let RuntimeCommand::Request {
            request_id, reply, ..
        } = commands.recv().await.expect("config request")
        else {
            panic!("expected config request")
        };
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "config": {
                        "api_config_id": api_config_id,
                        "display_name": "test",
                        "api_type": "CHAT_COMPLETIONS",
                        "base_url": "https://provider.example/v1/",
                        "model": "test",
                        "api_key_secret_ref": wrong_id,
                        "enabled": true,
                        "created_at": "2026-08-30T00:00:00Z",
                        "updated_at": "2026-08-30T00:00:00Z"
                    }
                }),
            )))
            .expect("config request remains active");

        let error = running
            .await
            .expect("join turn task")
            .expect_err("wrong credential kind must fail");
        assert_eq!(
            serde_json::to_value(error).unwrap()["code"],
            "CREDENTIAL_KIND_MISMATCH"
        );
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(25), commands.recv())
                .await
                .is_err()
        );
    });
}

#[test]
fn runtime_request_debug_redacts_agent_api_key() {
    let request = RuntimeRequest::secret(object(json!({
        "method": "agent.turn.run",
        "params": {"api_key_b64": "AGENT-KEY-MARKER"}
    })));

    let rendered = format!("{request:?}");
    assert!(!rendered.contains("AGENT-KEY-MARKER"));
    assert!(rendered.contains("<redacted>"));
}
