#![cfg(target_os = "windows")]

use std::{collections::VecDeque, sync::Mutex};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use harness_shell_lib::{
    commands::run_agent_turn_with_dependencies,
    runtime::{
        PtyInput, PtyInputResult, RunAgentTurnRequest, RuntimeClient, RuntimeClientError,
        RuntimeHttpRequest, RuntimeRequestBody,
    },
    vault::{CredentialKind, SecretVault, VaultState},
};
use reqwest::Method;
use serde_json::{json, Value};
use tempfile::tempdir;
use uuid::Uuid;

#[derive(Debug)]
struct RequestSnapshot {
    method: Method,
    path: String,
    body: Option<Value>,
}

struct FakeAgentRuntime {
    requests: Mutex<Vec<RequestSnapshot>>,
    responses: Mutex<VecDeque<Value>>,
}

impl FakeAgentRuntime {
    fn new(responses: Vec<Value>) -> Self {
        Self {
            requests: Mutex::new(Vec::new()),
            responses: Mutex::new(responses.into()),
        }
    }
}

impl RuntimeClient for FakeAgentRuntime {
    async fn execute<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeHttpRequest,
    {
        let body = request.body()?;
        let body = match &body {
            RuntimeRequestBody::Empty => None,
            RuntimeRequestBody::Json(bytes) => Some(serde_json::from_slice(bytes).unwrap()),
            RuntimeRequestBody::Binary { .. } => panic!("Agent command cannot send binary body"),
        };
        self.requests.lock().unwrap().push(RequestSnapshot {
            method: request.method(),
            path: request.path(),
            body,
        });
        serde_json::from_value(self.responses.lock().unwrap().pop_front().unwrap()).map_err(|_| {
            RuntimeClientError::HttpContract {
                reason: "fake Agent response type mismatch",
            }
        })
    }

    async fn send_pty_input(
        &self,
        _request: PtyInput,
    ) -> Result<PtyInputResult, RuntimeClientError> {
        Err(RuntimeClientError::WebSocketClosed)
    }
}

fn config_response(api_config_id: Uuid, secret_ref: impl ToString) -> Value {
    json!({
        "request_id": Uuid::new_v4(),
        "config": {
            "api_config_id": api_config_id,
            "display_name": "test provider",
            "api_type": "RESPONSES",
            "base_url": "https://provider.example/v1/",
            "model": "test-model",
            "api_key_secret_ref": secret_ref.to_string(),
            "enabled": true,
            "created_at": "2026-08-30T00:00:00Z",
            "updated_at": "2026-08-30T00:00:00Z"
        }
    })
}

#[tokio::test]
async fn agent_turn_reads_config_normally_then_sends_only_the_turn_with_secret() {
    let directory = tempdir().unwrap();
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).unwrap();
    let api_key_id = vault
        .put_secret(CredentialKind::ApiKey, b"model-key-marker")
        .unwrap();
    let vault = VaultState::new(vault);
    let api_config_id = Uuid::new_v4();
    let ssh_session_id = Uuid::new_v4();
    let runtime = FakeAgentRuntime::new(vec![
        config_response(api_config_id, api_key_id),
        json!({
            "request_id": Uuid::new_v4(),
            "conversation_id": Uuid::new_v4(),
            "agent_run_id": Uuid::new_v4(),
            "status": "COMPLETED",
            "final_text": "done",
            "react_iteration": 0,
            "error_code": null
        }),
    ]);

    let result = run_agent_turn_with_dependencies(
        &runtime,
        &vault,
        None,
        ssh_session_id,
        api_config_id,
        "inspect the host".to_owned(),
    )
    .await
    .unwrap();

    assert_eq!(result.final_text.as_deref(), Some("done"));
    let requests = runtime.requests.lock().unwrap();
    assert_eq!(requests[0].method, Method::GET);
    assert_eq!(
        requests[0].path,
        format!("/v1/agent/api-configs/{api_config_id}")
    );
    assert!(requests[0].body.is_none());
    assert_eq!(requests[1].method, Method::POST);
    assert_eq!(requests[1].path, "/v1/agent/turns");
    let body = requests[1].body.as_ref().unwrap();
    assert_eq!(body["api_key_credential_id"], api_key_id.to_string());
    assert_eq!(body["api_key_b64"], STANDARD.encode(b"model-key-marker"));
    assert_eq!(body["ssh_session_id"], ssh_session_id.to_string());
}

#[tokio::test]
async fn non_api_credential_fails_before_agent_turn_request() {
    let directory = tempdir().unwrap();
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).unwrap();
    let wrong_id = vault
        .put_secret(CredentialKind::SshPassword, b"wrong-kind")
        .unwrap();
    let vault = VaultState::new(vault);
    let api_config_id = Uuid::new_v4();
    let runtime = FakeAgentRuntime::new(vec![config_response(api_config_id, wrong_id)]);

    let error = run_agent_turn_with_dependencies(
        &runtime,
        &vault,
        None,
        Uuid::new_v4(),
        api_config_id,
        "inspect".to_owned(),
    )
    .await
    .unwrap_err();

    assert_eq!(
        serde_json::to_value(error).unwrap()["code"],
        "CREDENTIAL_KIND_MISMATCH"
    );
    assert_eq!(runtime.requests.lock().unwrap().len(), 1);
}

#[test]
fn runtime_request_debug_redacts_agent_api_key() {
    let request = RunAgentTurnRequest::new(
        None,
        Uuid::new_v4(),
        Uuid::new_v4(),
        harness_shell_lib::vault::CredentialId::new(),
        "AGENT-KEY-MARKER".to_owned(),
        "inspect".to_owned(),
    );

    let rendered = format!("{request:?}");
    assert!(!rendered.contains("AGENT-KEY-MARKER"));
    assert!(rendered.contains("<redacted>"));
}
