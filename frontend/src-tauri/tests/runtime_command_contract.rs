use std::{collections::VecDeque, sync::Mutex};

use harness_shell_lib::{
    commands::{
        close_pty_with_runtime, confirm_host_key_with_runtime, connect_ssh_with_dependencies,
        create_connection_with_runtime, create_model_api_config_with_runtime,
        delete_connection_with_runtime, delete_model_api_config_with_runtime,
        disconnect_ssh_with_runtime, inspect_host_key_with_dependencies,
        list_connections_with_runtime, list_model_api_configs_with_runtime, open_pty_with_runtime,
        replace_host_key_with_runtime, resize_pty_with_runtime, update_connection_with_runtime,
        update_model_api_config_with_runtime, write_pty_with_runtime, AgentRunStatus, ApiType,
        AuthKind, ConnectionProfileInput, HostKeyCandidate, ModelApiConfigInput,
    },
    runtime::{
        ClosePtyRequest, ConfirmHostKeyRequest, ConnectSshRequest, CreateAgentApiConfigRequest,
        CreateConnectionRequest, DeleteAgentApiConfigRequest, DeleteConnectionRequest,
        DisconnectSshRequest, GetAgentApiConfigRequest, GetConnectionRequest,
        InspectHostKeyRequest, ListAgentApiConfigsRequest, ListConnectionsRequest, OpenPtyRequest,
        PtyInput, PtyInputResult, ReplaceHostKeyRequest, ResizePtyRequest, RunAgentTurnRequest,
        RuntimeClient, RuntimeClientError, RuntimeHttpRequest, RuntimeRequestBody,
        UpdateAgentApiConfigRequest, UpdateConnectionRequest,
    },
    vault::{CredentialId, CredentialKind, SecretVault, VaultState},
};
use reqwest::{Method, StatusCode};
use serde_json::json;
use uuid::Uuid;

fn assert_shape<R: RuntimeHttpRequest>(
    request: &R,
    method: Method,
    path: &str,
    status: StatusCode,
) {
    assert_eq!(request.method(), method);
    assert_eq!(request.path(), path);
    assert_eq!(request.success_status(), status);
}

fn json_request_body<R: RuntimeHttpRequest>(request: &R) -> serde_json::Value {
    let body = request.body().unwrap();
    let RuntimeRequestBody::Json(bytes) = &body else {
        panic!("expected JSON request body")
    };
    serde_json::from_slice(bytes).unwrap()
}

fn connection_input() -> ConnectionProfileInput {
    ConnectionProfileInput {
        display_name: "primary".to_owned(),
        group_name: Some("prod".to_owned()),
        host: "host.example".to_owned(),
        port: 22,
        username: "alice".to_owned(),
        auth_kind: AuthKind::Password,
        credential_id: CredentialId::new(),
        passphrase_credential_id: None,
        proxy_jump_id: None,
        favorite: true,
    }
}

fn host_key_candidate(connection_id: Uuid) -> HostKeyCandidate {
    HostKeyCandidate {
        connection_id,
        host: "host.example".to_owned(),
        port: 22,
        key_algorithm: "ssh-ed25519".to_owned(),
        fingerprint_sha256: "SHA256:test".to_owned(),
        public_key_openssh_b64: "c3NoLWVkMjU1MTkgQUFBQQ==".to_owned(),
    }
}

fn agent_config_input() -> ModelApiConfigInput {
    ModelApiConfigInput {
        display_name: "provider".to_owned(),
        api_type: ApiType::Responses,
        base_url: "https://provider.example/v1/".to_owned(),
        model: "test-model".to_owned(),
        api_key_secret_ref: CredentialId::new(),
        enabled: true,
    }
}

fn ssh_status_response(connection_id: Uuid, ssh_session_id: Uuid) -> serde_json::Value {
    json!({
        "request_id": Uuid::new_v4(),
        "status": {
            "connection_id": connection_id,
            "state": "READY",
            "session_id": ssh_session_id,
            "error_code": null,
            "recoverable": false,
            "correlation_id": Uuid::new_v4(),
            "host_key_candidate": null,
            "trusted_fingerprint_sha256": "SHA256:test"
        }
    })
}

#[derive(Default)]
struct FakeRuntimeClient {
    requests: Mutex<Vec<RequestSnapshot>>,
    websocket_inputs: Mutex<Vec<(Uuid, usize)>>,
    responses: Mutex<VecDeque<serde_json::Value>>,
}

#[derive(Debug, PartialEq)]
struct RequestSnapshot {
    method: Method,
    path: String,
    body: Option<serde_json::Value>,
}

impl FakeRuntimeClient {
    fn with_responses(responses: Vec<serde_json::Value>) -> Self {
        Self {
            responses: Mutex::new(responses.into()),
            ..Self::default()
        }
    }
}

impl RuntimeClient for FakeRuntimeClient {
    async fn execute<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeHttpRequest,
    {
        let body = request.body()?;
        let body = match &body {
            RuntimeRequestBody::Empty => None,
            RuntimeRequestBody::Json(bytes) => {
                Some(serde_json::from_slice(bytes).map_err(|_| {
                    RuntimeClientError::HttpContract {
                        reason: "fake request body mismatch",
                    }
                })?)
            }
            RuntimeRequestBody::Binary { .. } => {
                return Err(RuntimeClientError::HttpContract {
                    reason: "unexpected binary command body",
                })
            }
        };
        self.requests.lock().unwrap().push(RequestSnapshot {
            method: request.method(),
            path: request.path(),
            body,
        });
        let response = self
            .responses
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| {
                json!({
                    "request_id": Uuid::new_v4(),
                    "connections": [],
                })
            });
        serde_json::from_value(response).map_err(|_| RuntimeClientError::HttpContract {
            reason: "fake response type mismatch",
        })
    }

    async fn send_pty_input(
        &self,
        request: PtyInput,
    ) -> Result<PtyInputResult, RuntimeClientError> {
        let byte_count = request.data_len();
        self.websocket_inputs
            .lock()
            .unwrap()
            .push((request.pty_session_id, byte_count));
        Ok(PtyInputResult {
            pty_session_id: request.pty_session_id,
            accepted_bytes: byte_count as u32,
        })
    }
}

#[test]
fn connection_list_request_has_one_fixed_http_shape() {
    let request = ListConnectionsRequest;

    assert_eq!(request.method(), Method::GET);
    assert_eq!(request.path(), "/v1/connections");
    assert!(matches!(request.body().unwrap(), RuntimeRequestBody::Empty));
    assert_eq!(request.success_status(), StatusCode::OK);
}

#[tokio::test]
async fn list_connections_command_uses_the_sealed_connection_request() {
    let runtime = FakeRuntimeClient::default();

    let connections = list_connections_with_runtime(&runtime).await.unwrap();

    assert!(connections.is_empty());
    assert_eq!(
        *runtime.requests.lock().unwrap(),
        [RequestSnapshot {
            method: Method::GET,
            path: "/v1/connections".to_owned(),
            body: None,
        }]
    );
}

#[tokio::test]
async fn write_pty_uses_websocket_without_an_http_request() {
    let runtime = FakeRuntimeClient::default();
    let pty_session_id = Uuid::new_v4();

    let accepted = write_pty_with_runtime(&runtime, pty_session_id, "YQ==".to_owned())
        .await
        .unwrap();

    assert_eq!(accepted, 1);
    assert!(runtime.requests.lock().unwrap().is_empty());
    assert_eq!(
        *runtime.websocket_inputs.lock().unwrap(),
        [(pty_session_id, 1)]
    );
}

fn connection_response(connection_id: Uuid, input: &ConnectionProfileInput) -> serde_json::Value {
    json!({
        "request_id": Uuid::new_v4(),
        "connection": {
            "connection_id": connection_id,
            "version": 1,
            "display_name": input.display_name,
            "group_name": input.group_name,
            "host": input.host,
            "port": input.port,
            "username": input.username,
            "auth_kind": "password",
            "credential_id": input.credential_id.to_string(),
            "passphrase_credential_id": null,
            "proxy_jump_id": null,
            "favorite": input.favorite,
            "created_at": "2026-09-02T00:00:00Z",
            "updated_at": "2026-09-02T00:00:00Z"
        }
    })
}

#[tokio::test]
async fn connection_profile_commands_route_through_typed_requests() {
    let connection_id = Uuid::new_v4();
    let create_input = connection_input();
    let update_input = connection_input();
    let runtime = FakeRuntimeClient::with_responses(vec![
        connection_response(connection_id, &create_input),
        connection_response(connection_id, &update_input),
        json!({"request_id": Uuid::new_v4(), "deleted": true}),
    ]);

    create_connection_with_runtime(&runtime, create_input)
        .await
        .unwrap();
    update_connection_with_runtime(&runtime, connection_id, update_input)
        .await
        .unwrap();
    assert!(delete_connection_with_runtime(&runtime, connection_id)
        .await
        .unwrap());

    let requests = runtime.requests.lock().unwrap();
    assert_eq!(requests[0].method, Method::POST);
    assert_eq!(requests[0].path, "/v1/connections");
    assert_eq!(
        requests[0].body.as_ref().unwrap()["display_name"],
        "primary"
    );
    assert_eq!(requests[1].method, Method::PATCH);
    assert_eq!(requests[1].path, format!("/v1/connections/{connection_id}"));
    assert!(requests[1]
        .body
        .as_ref()
        .unwrap()
        .get("connection_id")
        .is_none());
    assert_eq!(requests[2].method, Method::DELETE);
    assert_eq!(requests[2].path, format!("/v1/connections/{connection_id}"));
    assert!(requests[2].body.is_none());
}

fn host_key_response(connection_id: Uuid) -> serde_json::Value {
    json!({
        "request_id": Uuid::new_v4(),
        "host_key": {
            "host_key_id": Uuid::new_v4(),
            "connection_id": connection_id,
            "key_algorithm": "ssh-ed25519",
            "fingerprint_sha256": "SHA256:test",
            "public_key_openssh_b64": "c3NoLWVkMjU1MTkgQUFBQQ==",
            "status": "active",
            "confirmed_at": "2026-09-02T00:00:00Z",
            "replaced_at": null
        }
    })
}

#[tokio::test]
async fn host_key_decisions_route_through_typed_requests() {
    let connection_id = Uuid::new_v4();
    let runtime = FakeRuntimeClient::with_responses(vec![
        host_key_response(connection_id),
        host_key_response(connection_id),
    ]);

    confirm_host_key_with_runtime(&runtime, host_key_candidate(connection_id))
        .await
        .unwrap();
    replace_host_key_with_runtime(
        &runtime,
        host_key_candidate(connection_id),
        "SHA256:old".to_owned(),
    )
    .await
    .unwrap();

    let requests = runtime.requests.lock().unwrap();
    assert_eq!(requests[0].path, "/v1/host-key-confirmations");
    assert_eq!(requests[1].path, "/v1/host-key-replacements");
    assert_eq!(
        requests[1].body.as_ref().unwrap()["expected_old_fingerprint"],
        "SHA256:old"
    );
}

fn pty_response(pty_session_id: Uuid, ssh_session_id: Uuid) -> serde_json::Value {
    json!({
        "request_id": Uuid::new_v4(),
        "pty_session": {
            "pty_session_id": pty_session_id,
            "ssh_session_id": ssh_session_id,
            "connection_id": Uuid::new_v4(),
            "cols": 80,
            "rows": 24,
            "state": "OPEN"
        }
    })
}

#[tokio::test]
async fn pty_lifecycle_commands_route_through_http_control_requests() {
    let ssh_session_id = Uuid::new_v4();
    let pty_session_id = Uuid::new_v4();
    let runtime = FakeRuntimeClient::with_responses(vec![
        pty_response(pty_session_id, ssh_session_id),
        pty_response(pty_session_id, ssh_session_id),
        pty_response(pty_session_id, ssh_session_id),
    ]);

    open_pty_with_runtime(&runtime, ssh_session_id, 80, 24)
        .await
        .unwrap();
    resize_pty_with_runtime(&runtime, pty_session_id, 120, 40)
        .await
        .unwrap();
    close_pty_with_runtime(&runtime, pty_session_id)
        .await
        .unwrap();

    let requests = runtime.requests.lock().unwrap();
    assert_eq!(requests[0].path, "/v1/pty/sessions");
    assert_eq!(
        requests[1].path,
        format!("/v1/pty/sessions/{pty_session_id}/resize")
    );
    assert_eq!(requests[1].body.as_ref().unwrap()["cols"], 120);
    assert_eq!(
        requests[2].path,
        format!("/v1/pty/sessions/{pty_session_id}")
    );
}

fn agent_config_response(api_config_id: Uuid, input: &ModelApiConfigInput) -> serde_json::Value {
    json!({
        "request_id": Uuid::new_v4(),
        "config": {
            "api_config_id": api_config_id,
            "display_name": input.display_name,
            "api_type": "RESPONSES",
            "base_url": input.base_url,
            "model": input.model,
            "api_key_secret_ref": input.api_key_secret_ref.to_string(),
            "enabled": input.enabled,
            "created_at": "2026-09-02T00:00:00Z",
            "updated_at": "2026-09-02T00:00:00Z"
        }
    })
}

#[tokio::test]
async fn agent_config_commands_route_through_typed_requests() {
    let api_config_id = Uuid::new_v4();
    let create_input = agent_config_input();
    let update_input = agent_config_input();
    let list_config = agent_config_response(api_config_id, &create_input)["config"].clone();
    let runtime = FakeRuntimeClient::with_responses(vec![
        json!({"request_id": Uuid::new_v4(), "configs": [list_config]}),
        agent_config_response(api_config_id, &create_input),
        agent_config_response(api_config_id, &update_input),
        json!({"request_id": Uuid::new_v4(), "deleted": true}),
    ]);

    assert_eq!(
        list_model_api_configs_with_runtime(&runtime)
            .await
            .unwrap()
            .len(),
        1
    );
    create_model_api_config_with_runtime(&runtime, create_input)
        .await
        .unwrap();
    update_model_api_config_with_runtime(&runtime, api_config_id, update_input)
        .await
        .unwrap();
    assert!(
        delete_model_api_config_with_runtime(&runtime, api_config_id)
            .await
            .unwrap()
    );

    let requests = runtime.requests.lock().unwrap();
    assert_eq!(requests[0].path, "/v1/agent/api-configs");
    assert_eq!(requests[0].method, Method::GET);
    assert_eq!(requests[1].method, Method::POST);
    assert_eq!(
        requests[2].path,
        format!("/v1/agent/api-configs/{api_config_id}")
    );
    assert_eq!(requests[2].method, Method::PATCH);
    assert_eq!(requests[3].method, Method::DELETE);
}

#[tokio::test]
async fn ssh_connect_reads_profile_before_resolving_vault_secret_and_posting() {
    let directory = tempfile::tempdir().unwrap();
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).unwrap();
    let credential_id = vault
        .put_secret(CredentialKind::SshPassword, b"password-marker")
        .unwrap();
    let vault = VaultState::new(vault);
    let connection_id = Uuid::new_v4();
    let ssh_session_id = Uuid::new_v4();
    let input = ConnectionProfileInput {
        credential_id,
        ..connection_input()
    };
    let runtime = FakeRuntimeClient::with_responses(vec![
        connection_response(connection_id, &input),
        ssh_status_response(connection_id, ssh_session_id),
        ssh_status_response(connection_id, ssh_session_id),
    ]);

    connect_ssh_with_dependencies(&runtime, &vault, connection_id)
        .await
        .unwrap();
    disconnect_ssh_with_runtime(&runtime, ssh_session_id)
        .await
        .unwrap();

    let requests = runtime.requests.lock().unwrap();
    assert_eq!(requests[0].method, Method::GET);
    assert_eq!(requests[0].path, format!("/v1/connections/{connection_id}"));
    assert_eq!(requests[1].method, Method::POST);
    assert_eq!(requests[1].path, "/v1/ssh/sessions");
    assert_eq!(requests[1].body.as_ref().unwrap()["profile_version"], 1);
    assert_eq!(
        requests[1].body.as_ref().unwrap()["password_b64"],
        "cGFzc3dvcmQtbWFya2Vy"
    );
    assert_eq!(
        requests[2].path,
        format!("/v1/ssh/sessions/{ssh_session_id}")
    );
    assert!(requests[2].body.is_none());
}

#[tokio::test]
async fn host_key_inspection_reads_current_profile_before_typed_inspection() {
    let directory = tempfile::tempdir().unwrap();
    let vault = VaultState::new(SecretVault::open(directory.path().join("vault.sqlite3")).unwrap());
    let connection_id = Uuid::new_v4();
    let input = connection_input();
    let runtime = FakeRuntimeClient::with_responses(vec![
        connection_response(connection_id, &input),
        ssh_status_response(connection_id, Uuid::new_v4()),
    ]);

    inspect_host_key_with_dependencies(&runtime, &vault, connection_id)
        .await
        .unwrap();

    let requests = runtime.requests.lock().unwrap();
    assert_eq!(requests[0].path, format!("/v1/connections/{connection_id}"));
    assert_eq!(requests[1].path, "/v1/host-key-inspections");
    assert_eq!(
        requests[1].body.as_ref().unwrap(),
        &json!({"connection_id": connection_id})
    );
}

#[test]
fn non_sftp_control_requests_have_frozen_routes_and_bodies() {
    let connection_id = Uuid::new_v4();
    assert_shape(
        &GetConnectionRequest { connection_id },
        Method::GET,
        &format!("/v1/connections/{connection_id}"),
        StatusCode::OK,
    );
    let create = CreateConnectionRequest(connection_input());
    assert_shape(
        &create,
        Method::POST,
        "/v1/connections",
        StatusCode::CREATED,
    );
    assert_eq!(json_request_body(&create)["display_name"], "primary");
    assert_shape(
        &UpdateConnectionRequest {
            connection_id,
            input: connection_input(),
        },
        Method::PATCH,
        &format!("/v1/connections/{connection_id}"),
        StatusCode::OK,
    );
    assert_shape(
        &DeleteConnectionRequest { connection_id },
        Method::DELETE,
        &format!("/v1/connections/{connection_id}"),
        StatusCode::OK,
    );

    let candidate = host_key_candidate(connection_id);
    assert_shape(
        &ConfirmHostKeyRequest(candidate),
        Method::POST,
        "/v1/host-key-confirmations",
        StatusCode::CREATED,
    );
    let replacement = ReplaceHostKeyRequest {
        candidate: host_key_candidate(connection_id),
        expected_old_fingerprint: "SHA256:old".to_owned(),
    };
    assert_eq!(
        json_request_body(&replacement)["expected_old_fingerprint"],
        "SHA256:old"
    );
    assert_shape(
        &replacement,
        Method::POST,
        "/v1/host-key-replacements",
        StatusCode::OK,
    );
    assert_shape(
        &InspectHostKeyRequest {
            connection_id,
            jump: None,
        },
        Method::POST,
        "/v1/host-key-inspections",
        StatusCode::OK,
    );

    let ssh_session_id = Uuid::new_v4();
    let connect = ConnectSshRequest::password(connection_id, 7, "c2VjcmV0".to_owned(), None);
    assert_shape(
        &connect,
        Method::POST,
        "/v1/ssh/sessions",
        StatusCode::CREATED,
    );
    assert!(!format!("{connect:?}").contains("c2VjcmV0"));
    assert_eq!(json_request_body(&connect)["profile_version"], 7);
    assert_shape(
        &DisconnectSshRequest { ssh_session_id },
        Method::DELETE,
        &format!("/v1/ssh/sessions/{ssh_session_id}"),
        StatusCode::OK,
    );

    let pty_session_id = Uuid::new_v4();
    assert_shape(
        &OpenPtyRequest {
            ssh_session_id,
            cols: 80,
            rows: 24,
        },
        Method::POST,
        "/v1/pty/sessions",
        StatusCode::CREATED,
    );
    assert_shape(
        &ResizePtyRequest {
            pty_session_id,
            cols: 120,
            rows: 40,
        },
        Method::POST,
        &format!("/v1/pty/sessions/{pty_session_id}/resize"),
        StatusCode::OK,
    );
    assert_shape(
        &ClosePtyRequest { pty_session_id },
        Method::DELETE,
        &format!("/v1/pty/sessions/{pty_session_id}"),
        StatusCode::OK,
    );

    let api_config_id = Uuid::new_v4();
    assert_shape(
        &ListAgentApiConfigsRequest,
        Method::GET,
        "/v1/agent/api-configs",
        StatusCode::OK,
    );
    assert_shape(
        &GetAgentApiConfigRequest { api_config_id },
        Method::GET,
        &format!("/v1/agent/api-configs/{api_config_id}"),
        StatusCode::OK,
    );
    assert_shape(
        &CreateAgentApiConfigRequest(agent_config_input()),
        Method::POST,
        "/v1/agent/api-configs",
        StatusCode::CREATED,
    );
    assert_shape(
        &UpdateAgentApiConfigRequest {
            api_config_id,
            input: agent_config_input(),
        },
        Method::PATCH,
        &format!("/v1/agent/api-configs/{api_config_id}"),
        StatusCode::OK,
    );
    assert_shape(
        &DeleteAgentApiConfigRequest { api_config_id },
        Method::DELETE,
        &format!("/v1/agent/api-configs/{api_config_id}"),
        StatusCode::OK,
    );
    let turn = RunAgentTurnRequest::new(
        None,
        ssh_session_id,
        api_config_id,
        CredentialId::new(),
        "QUdFTlQtS0VZLU1BUktFUg==".to_owned(),
        "inspect".to_owned(),
    );
    assert_shape(&turn, Method::POST, "/v1/agent/turns", StatusCode::OK);
    assert!(!format!("{turn:?}").contains("QUdFTlQtS0VZLU1BUktFUg=="));
    assert_eq!(json_request_body(&turn)["user_message"], "inspect");

    let _ = AgentRunStatus::Completed;
}
