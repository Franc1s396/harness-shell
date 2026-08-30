use harness_shell_lib::{
    protocol::{FrameEnvelope, MessageType, Sensitivity},
    sftp::models::{
        require_js_safe, RecoveryAction, RemoteEntry, JS_SAFE_INTEGER_MAX, SFTP_CHUNK_BYTES,
    },
    sftp::protocol::ManualSftpRuntimeClient,
    sidecar::broker::{project_runtime_event, runtime_broker_channel, BrokerError, RuntimeCommand},
};
use serde_json::{json, Map, Value};
use time::OffsetDateTime;
use uuid::Uuid;

fn object(value: Value) -> Map<String, Value> {
    value
        .as_object()
        .expect("fixture must be a JSON object")
        .clone()
}

fn operation_progress_payload() -> Map<String, Value> {
    object(json!({
        "event": "manual_sftp.operation.progress",
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "kind": "recursive_delete",
        "phase": "deleting",
        "display_name": "cache",
        "remote_path": "/home/demo/cache",
        "host_label": "demo-host",
        "items_completed": 12,
        "items_total": 30,
        "cancellable": false
    }))
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

fn error_response(request_id: Uuid, error_code: &str) -> FrameEnvelope {
    FrameEnvelope {
        protocol_version: 1,
        message_type: MessageType::Error,
        request_id,
        task_id: None,
        workflow_run_id: None,
        sequence: 1,
        timestamp: OffsetDateTime::now_utc(),
        sensitivity: Sensitivity::Normal,
        payload: object(json!({"error_code": error_code})),
    }
}

#[test]
fn remote_entry_requires_decimal_mtime_and_rejects_unknown_fields() {
    let value = json!({
        "name": "data.txt",
        "path": "/home/demo/data.txt",
        "entry_type": "file",
        "size": 12,
        "mode": 33188,
        "mtime_ns": "1770000000000000000",
        "link_target": null
    });
    let entry: RemoteEntry = serde_json::from_value(value.clone()).unwrap();
    assert_eq!(entry.size, Some(12));

    let mut invalid_mtime = value.clone();
    invalid_mtime["mtime_ns"] = json!(1770000000000000000_u64);
    assert!(serde_json::from_value::<RemoteEntry>(invalid_mtime).is_err());

    let mut unknown = value;
    unknown["local_path"] = json!(r"C:\secret\data.txt");
    assert!(serde_json::from_value::<RemoteEntry>(unknown).is_err());
}

#[test]
fn byte_counts_are_bounded_for_the_webview_contract() {
    assert_eq!(SFTP_CHUNK_BYTES, 262_144);
    assert_eq!(
        require_js_safe(JS_SAFE_INTEGER_MAX).unwrap(),
        JS_SAFE_INTEGER_MAX
    );
    let error = require_js_safe(JS_SAFE_INTEGER_MAX + 1).unwrap_err();
    assert_eq!(error.code(), "SFTP_FILE_SIZE_UNSUPPORTED");
}

#[test]
fn decoded_upload_chunk_overflow_uses_the_approved_stable_code() {
    let (_broker, _commands) = runtime_broker_channel();
    let client = ManualSftpRuntimeClient::new(_broker);
    let error = tauri::async_runtime::block_on(async {
        client
            .upload_chunk(Uuid::new_v4(), 0, 0, &vec![0_u8; SFTP_CHUNK_BYTES + 1])
            .await
            .unwrap_err()
    });

    assert_eq!(error.code(), "SFTP_CHUNK_LIMIT_EXCEEDED");
}

#[test]
fn runtime_preserves_distinct_safe_chunk_error_codes() {
    tauri::async_runtime::block_on(async {
        for error_code in ["SFTP_CHUNK_INVALID", "SFTP_CHUNK_LIMIT_EXCEEDED"] {
            let (broker, mut commands) = runtime_broker_channel();
            let client = ManualSftpRuntimeClient::new(broker);
            let call = tokio::spawn(async move {
                client
                    .download_begin(Uuid::new_v4(), Uuid::new_v4(), "/home/demo/file")
                    .await
            });

            let RuntimeCommand::Request {
                request_id, reply, ..
            } = commands.recv().await.unwrap()
            else {
                panic!("expected runtime request");
            };
            reply
                .send(Ok(error_response(request_id, error_code)))
                .unwrap();

            assert_eq!(call.await.unwrap().unwrap_err().code(), error_code);
        }
    });
}

#[test]
fn manual_sftp_progress_routes_only_to_the_main_operation_event() {
    let projection = project_runtime_event(&operation_progress_payload()).unwrap();
    assert_eq!(projection.webview_event, "manual-sftp://operation-state");
    assert!(!projection.payload.to_string().contains("local_path"));
    assert_eq!(projection.payload["kind"], "recursive_delete");
}

#[test]
fn malformed_manual_sftp_progress_is_a_protocol_failure() {
    let mut payload = operation_progress_payload();
    payload.insert("local_path".to_owned(), json!(r"C:\secret\part.bin"));
    assert_eq!(project_runtime_event(&payload), Err(BrokerError::Protocol));

    payload.remove("local_path");
    payload.insert("cancellable".to_owned(), json!(true));
    assert_eq!(project_runtime_event(&payload), Err(BrokerError::Protocol));
}

#[test]
fn ssh_events_keep_the_existing_route_and_unknown_events_fail_closed() {
    let ssh = project_runtime_event(&object(json!({
        "event": "ssh.connection.status",
        "connection_id": "00000000-0000-4000-8000-000000000002"
    })))
    .unwrap();
    assert_eq!(ssh.webview_event, "ssh://event");

    let unknown = project_runtime_event(&object(json!({"event": "shell.execute"})));
    assert_eq!(unknown, Err(BrokerError::UnknownEvent));
}

#[test]
fn runtime_client_builds_the_exact_open_request_and_decodes_strictly() {
    tauri::async_runtime::block_on(async {
        let ssh_session_id = Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap();
        let connection_id = Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap();
        let (broker, mut commands) = runtime_broker_channel();
        let client = ManualSftpRuntimeClient::new(broker);
        let call = tokio::spawn(async move { client.open(ssh_session_id).await });

        let RuntimeCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected runtime request");
        };
        assert_eq!(request.sensitivity, Sensitivity::Normal);
        assert_eq!(
            request.payload,
            object(json!({
                "method": "manual_sftp.open",
                "params": {"ssh_session_id": ssh_session_id}
            }))
        );
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "context": {
                        "ssh_session_id": ssh_session_id,
                        "connection_id": connection_id,
                        "home": "/home/demo",
                        "host_label": "demo-host",
                        "sftp_version": 3
                    }
                }),
            )))
            .unwrap();

        let context = call.await.unwrap().unwrap();
        assert_eq!(context.connection_id, connection_id);
        assert_eq!(context.home, "/home/demo");
    });
}

#[test]
fn chunk_requests_are_secret_and_unknown_response_fields_fail_closed() {
    tauri::async_runtime::block_on(async {
        let operation_id = Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap();
        let (broker, mut commands) = runtime_broker_channel();
        let client = ManualSftpRuntimeClient::new(broker);
        let call =
            tokio::spawn(
                async move { client.upload_chunk(operation_id, 0, 0, &[1_u8, 2, 3]).await },
            );

        let RuntimeCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected runtime request");
        };
        assert_eq!(request.sensitivity, Sensitivity::Secret);
        assert_eq!(request.payload["method"], "manual_sftp.upload.chunk");
        assert_eq!(request.payload["params"]["chunk_b64"], "AQID");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "next_sequence": 1,
                        "next_offset": 3,
                        "unexpected": true
                    }
                }),
            )))
            .unwrap();

        let error = call.await.unwrap().unwrap_err();
        assert_eq!(error.code(), "SIDECAR_RESPONSE_INVALID");
    });
}

#[test]
fn download_chunk_request_is_secret_and_empty_response_is_rejected() {
    tauri::async_runtime::block_on(async {
        let operation_id = Uuid::parse_str("00000000-0000-4000-8000-000000000004").unwrap();
        let (broker, mut commands) = runtime_broker_channel();
        let client = ManualSftpRuntimeClient::new(broker);
        let call = tokio::spawn(async move { client.download_chunk(operation_id, 2, 6).await });

        let RuntimeCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected runtime request");
        };
        assert_eq!(request.sensitivity, Sensitivity::Secret);
        assert_eq!(request.payload["method"], "manual_sftp.download.chunk");
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "chunk": {
                        "operation_id": operation_id,
                        "sequence": 2,
                        "offset": 6,
                        "chunk_b64": "",
                        "next_offset": 6,
                        "eof": true
                    }
                }),
            )))
            .unwrap();

        let error = call.await.unwrap().unwrap_err();
        assert_eq!(error.code(), "SIDECAR_RESPONSE_INVALID");
    });
}

#[test]
fn recovery_execute_sends_the_rust_selected_fresh_operation_id() {
    tauri::async_runtime::block_on(async {
        let recovery_id = Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap();
        let operation_id = Uuid::parse_str("00000000-0000-4000-8000-000000000006").unwrap();
        let (broker, mut commands) = runtime_broker_channel();
        let client = ManualSftpRuntimeClient::new(broker);
        let call = tokio::spawn(async move {
            client
                .recovery_execute(recovery_id, RecoveryAction::DeleteTemp, operation_id)
                .await
        });

        let RuntimeCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected runtime request");
        };
        assert_eq!(
            request.payload,
            object(json!({
                "method": "manual_sftp.recovery.execute",
                "params": {
                    "recovery_id": recovery_id,
                    "action": "delete_temp",
                    "operation_id": operation_id
                }
            }))
        );
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "recovery": {
                        "operation_id": operation_id,
                        "state": "succeeded",
                        "error_code": null,
                        "message": "Temporary file removed.",
                        "sha256": null,
                        "byte_count": null,
                        "recovery_id": null
                    }
                }),
            )))
            .unwrap();
        assert!(call.await.unwrap().is_ok());
    });
}

#[test]
fn delete_preflight_sends_the_rust_selected_operation_id() {
    tauri::async_runtime::block_on(async {
        let ssh_session_id = Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap();
        let operation_id = Uuid::parse_str("00000000-0000-4000-8000-000000000007").unwrap();
        let delete_plan_id = Uuid::parse_str("00000000-0000-4000-8000-000000000008").unwrap();
        let (broker, mut commands) = runtime_broker_channel();
        let client = ManualSftpRuntimeClient::new(broker);
        let call = tokio::spawn(async move {
            client
                .delete_preflight(operation_id, ssh_session_id, "/home/demo/tree")
                .await
        });

        let RuntimeCommand::Request {
            request_id,
            request,
            reply,
        } = commands.recv().await.unwrap()
        else {
            panic!("expected runtime request");
        };
        assert_eq!(request.payload["method"], "manual_sftp.delete.preflight");
        assert_eq!(
            request.payload["params"]["operation_id"],
            operation_id.to_string()
        );
        reply
            .send(Ok(response(
                request_id,
                json!({
                    "delete_plan": {
                        "delete_plan_id": delete_plan_id,
                        "operation_id": operation_id,
                        "root_path": "/home/demo/tree",
                        "root_snapshot": {
                            "path": "/home/demo/tree",
                            "exists": true,
                            "entry_type": "directory",
                            "size": null,
                            "mtime_ns": "1770000000000000000",
                            "sha256": null
                        },
                        "file_count": 0,
                        "directory_count": 1,
                        "symlink_count": 0,
                        "total_byte_count": 0,
                        "manifest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "complete": true
                    }
                }),
            )))
            .unwrap();

        assert_eq!(call.await.unwrap().unwrap().operation_id, operation_id);
    });
}
