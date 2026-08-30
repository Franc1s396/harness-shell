use harness_shell_lib::{
    protocol::{FrameEnvelope, MessageType, Sensitivity},
    sidecar::broker::{
        emit_runtime_event_projection, project_runtime_event, runtime_broker_channel,
        validate_runtime_event, BrokerError, PendingReplies, RuntimeCommand, RuntimeRequest,
    },
};
use serde_json::{json, Map, Value};
use std::time::Duration;
use time::OffsetDateTime;
use uuid::Uuid;

fn object(value: Value) -> Map<String, Value> {
    value
        .as_object()
        .expect("payload must be an object")
        .clone()
}

fn response(request_id: Uuid, method: &str) -> FrameEnvelope {
    FrameEnvelope {
        protocol_version: 1,
        message_type: MessageType::Response,
        request_id,
        task_id: None,
        workflow_run_id: None,
        sequence: 1,
        timestamp: OffsetDateTime::now_utc(),
        sensitivity: Sensitivity::Normal,
        payload: object(json!({"method": method})),
    }
}

#[test]
fn broker_correlates_out_of_order_replies() {
    tauri::async_runtime::block_on(async {
        let (broker, mut commands) = runtime_broker_channel();
        let first = tokio::spawn({
            let broker = broker.clone();
            async move {
                broker
                    .request(RuntimeRequest::normal(object(json!({
                        "method": "connections.list"
                    }))))
                    .await
            }
        });
        let second = tokio::spawn({
            let broker = broker.clone();
            async move {
                broker
                    .request(RuntimeRequest::normal(object(json!({
                        "method": "host_key.inspect"
                    }))))
                    .await
            }
        });

        let mut first_command = None;
        let mut second_command = None;
        for command in [
            commands.recv().await.expect("first broker command"),
            commands.recv().await.expect("second broker command"),
        ] {
            match command {
                RuntimeCommand::Request {
                    request_id,
                    request,
                    reply,
                } if request.payload["method"] == "connections.list" => {
                    first_command = Some((request_id, reply));
                }
                RuntimeCommand::Request {
                    request_id,
                    request,
                    reply,
                } if request.payload["method"] == "host_key.inspect" => {
                    second_command = Some((request_id, reply));
                }
                RuntimeCommand::Request { .. } => panic!("unexpected request method"),
                RuntimeCommand::Shutdown => panic!("unexpected shutdown"),
                RuntimeCommand::Cancel { .. } => panic!("unexpected cancel"),
            }
        }
        let (first_id, first_reply) = first_command.expect("connections.list command");
        let (second_id, second_reply) = second_command.expect("host_key.inspect command");

        second_reply
            .send(Ok(response(second_id, "host_key.inspect")))
            .expect("second receiver remains active");
        first_reply
            .send(Ok(response(first_id, "connections.list")))
            .expect("first receiver remains active");

        assert_eq!(
            second.await.unwrap().unwrap().payload["method"],
            "host_key.inspect"
        );
        assert_eq!(
            first.await.unwrap().unwrap().payload["method"],
            "connections.list"
        );
    });
}

#[test]
fn closed_broker_fails_without_fallback() {
    tauri::async_runtime::block_on(async {
        let (broker, commands) = runtime_broker_channel();
        drop(commands);

        let error = broker
            .request(RuntimeRequest::normal(object(json!({
                "method": "connections.list"
            }))))
            .await
            .unwrap_err();

        assert_eq!(error, BrokerError::Closed);
        assert_eq!(error.error_code(), "SIDECAR_BROKER_CLOSED");
    });
}

#[test]
fn secret_request_debug_is_redacted() {
    let marker = "BROKER-SECRET-MARKER-91d1";
    let request = RuntimeRequest::secret(object(json!({
        "method": "ssh.connect",
        "password": marker
    })));

    let rendered = format!("{request:?}");
    assert!(!rendered.contains(marker));
    assert!(rendered.contains("Secret"));
}

#[test]
fn only_known_ssh_events_can_reach_the_main_window() {
    for event in ["ssh.connection.status", "ssh.pty.output", "ssh.pty.closed"] {
        let payload = object(json!({"event": event}));
        validate_runtime_event(&payload).expect("known SSH event");
        assert_eq!(
            project_runtime_event(&payload).unwrap().webview_event,
            "ssh://event"
        );
    }

    let error = validate_runtime_event(&object(json!({"event": "shell.execute"}))).unwrap_err();
    assert_eq!(error, BrokerError::UnknownEvent);
}

#[test]
fn legal_manual_sftp_event_survives_webview_emission_failure() {
    let payload = object(json!({
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
    }));

    emit_runtime_event_projection(&payload, |_projection| Err::<(), ()>(()))
        .expect("a legal observation must not become a protocol failure");
}

#[test]
fn pending_reply_ledger_routes_by_request_id_and_rejects_unknown_ids() {
    tauri::async_runtime::block_on(async {
        let mut pending = PendingReplies::new();
        let first_id = Uuid::new_v4();
        let second_id = Uuid::new_v4();
        let (first_sender, first_receiver) = tokio::sync::oneshot::channel();
        let (second_sender, second_receiver) = tokio::sync::oneshot::channel();
        pending.insert(first_id, first_sender).unwrap();
        pending.insert(second_id, second_sender).unwrap();

        pending
            .complete(response(second_id, "second"))
            .expect("second response");
        pending
            .complete(response(first_id, "first"))
            .expect("first response");
        assert_eq!(
            second_receiver.await.unwrap().unwrap().payload["method"],
            "second"
        );
        assert_eq!(
            first_receiver.await.unwrap().unwrap().payload["method"],
            "first"
        );

        let error = pending.complete(response(Uuid::new_v4(), "unknown"));
        assert_eq!(error.unwrap_err(), BrokerError::UnknownResponse);
    });
}

#[test]
fn duplicate_pending_reply_id_does_not_replace_original_waiter() {
    tauri::async_runtime::block_on(async {
        let mut pending = PendingReplies::new();
        let request_id = Uuid::new_v4();
        let (original_sender, original_receiver) = tokio::sync::oneshot::channel();
        let (duplicate_sender, duplicate_receiver) = tokio::sync::oneshot::channel();
        pending.insert(request_id, original_sender).unwrap();
        assert_eq!(
            pending.insert(request_id, duplicate_sender).unwrap_err(),
            BrokerError::Protocol
        );

        pending
            .complete(response(request_id, "original"))
            .expect("original waiter remains registered");
        let original = tokio::time::timeout(Duration::from_millis(100), original_receiver)
            .await
            .expect("original waiter receives response")
            .unwrap()
            .unwrap();
        assert_eq!(original.payload["method"], "original");
        assert!(duplicate_receiver.await.is_err());
    });
}
