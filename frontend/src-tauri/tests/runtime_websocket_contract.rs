use std::{future::Future, time::Duration};

use futures_util::{SinkExt, StreamExt};
use harness_shell_lib::runtime::websocket::{
    PtyInput, RuntimeProjection, RuntimeWebSocketConnection, RuntimeWebSocketError,
    WEBSOCKET_QUEUE_CAPACITY,
};
use serde_json::{json, Value};
use tokio::{net::TcpListener, sync::mpsc};
use tokio_tungstenite::tungstenite::protocol::{frame::coding::CloseCode, CloseFrame};
use tokio_tungstenite::{accept_async, tungstenite::Message};
use uuid::Uuid;

async fn mock_server<F, Fut>(script: F) -> u16
where
    F: FnOnce(tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>) -> Fut + Send + 'static,
    Fut: Future<Output = ()> + Send + 'static,
{
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let websocket = accept_async(stream).await.unwrap();
        script(websocket).await;
    });
    port
}

async fn accept_initial_ping(
    websocket: &mut tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>,
) -> Uuid {
    let message = websocket.next().await.unwrap().unwrap();
    let value: Value = serde_json::from_str(message.to_text().unwrap()).unwrap();
    assert_eq!(value["schema_version"], 1);
    assert_eq!(value["type"], "runtime.ping");
    assert!(value["causation_id"].is_null());
    let message_id = Uuid::parse_str(value["message_id"].as_str().unwrap()).unwrap();
    websocket
        .send(Message::Text(
            json!({
                "schema_version": 1,
                "type": "runtime.pong",
                "message_id": Uuid::new_v4(),
                "causation_id": message_id,
                "timestamp": "2026-09-02T00:00:00Z",
                "payload": {"server_timestamp": "2026-09-02T00:00:00Z"}
            })
            .to_string()
            .into(),
        ))
        .await
        .unwrap();
    message_id
}

#[tokio::test]
async fn typed_ping_and_pty_input_result_are_correlated() {
    let pty_session_id = Uuid::new_v4();
    let port = mock_server(move |mut websocket| async move {
        accept_initial_ping(&mut websocket).await;
        let message = websocket.next().await.unwrap().unwrap();
        let value: Value = serde_json::from_str(message.to_text().unwrap()).unwrap();
        assert_eq!(value["type"], "pty.input");
        assert_eq!(
            value["payload"]["pty_session_id"],
            pty_session_id.to_string()
        );
        assert_eq!(value["payload"]["data_b64"], "aGk=");
        let input_id = value["message_id"].as_str().unwrap();
        websocket
            .send(Message::Text(
                json!({
                    "schema_version": 1,
                    "type": "pty.input_result",
                    "message_id": Uuid::new_v4(),
                    "causation_id": input_id,
                    "timestamp": "2026-09-02T00:00:01Z",
                    "payload": {
                        "pty_session_id": pty_session_id,
                        "accepted_bytes": 2,
                        "error_code": null
                    }
                })
                .to_string()
                .into(),
            ))
            .await
            .unwrap();
        let _ = websocket.next().await;
    })
    .await;
    let (projection_tx, _projection_rx) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);
    let connection = RuntimeWebSocketConnection::connect(port, projection_tx)
        .await
        .unwrap();
    let (handle, task) = connection.split();

    let result = handle
        .send_pty_input(PtyInput::new(pty_session_id, b"hi").unwrap())
        .await
        .unwrap();

    assert_eq!(result.pty_session_id, pty_session_id);
    assert_eq!(result.accepted_bytes, 2);
    handle.shutdown().await.unwrap();
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn invalid_event_fails_runtime_without_projecting_raw_payload() {
    let port = mock_server(|mut websocket| async move {
        accept_initial_ping(&mut websocket).await;
        websocket
            .send(Message::Text(
                r#"{"schema_version":1,"type":"unknown.event","message_id":"00000000-0000-4000-8000-000000000001","causation_id":null,"timestamp":"2026-09-02T00:00:00Z","payload":{}}"#
                    .into(),
            ))
            .await
            .unwrap();
    })
    .await;
    let (projection_tx, mut projection_rx) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);
    let connection = RuntimeWebSocketConnection::connect(port, projection_tx)
        .await
        .unwrap();
    let (_handle, task) = connection.split();

    let failure = task.await.unwrap().unwrap_err();

    assert!(matches!(failure, RuntimeWebSocketError::Contract { .. }));
    assert_eq!(failure.error_code(), "RUNTIME_WEBSOCKET_CONTRACT_FAILED");
    assert!(projection_rx.try_recv().is_err());
}

#[tokio::test]
async fn pty_output_is_projected_in_current_webview_shape() {
    let pty_session_id = Uuid::new_v4();
    let port = mock_server(move |mut websocket| async move {
        accept_initial_ping(&mut websocket).await;
        websocket
            .send(Message::Text(
                json!({
                    "schema_version": 1,
                    "type": "pty.output",
                    "message_id": Uuid::new_v4(),
                    "causation_id": null,
                    "timestamp": "2026-09-02T00:00:01Z",
                    "payload": {
                        "pty_session_id": pty_session_id,
                        "data_b64": "b2s=",
                        "stream_sequence": 1
                    }
                })
                .to_string()
                .into(),
            ))
            .await
            .unwrap();
        let _ = websocket.next().await;
    })
    .await;
    let (projection_tx, mut projection_rx) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);
    let connection = RuntimeWebSocketConnection::connect(port, projection_tx)
        .await
        .unwrap();
    let (handle, task) = connection.split();

    let RuntimeProjection { event, payload } =
        tokio::time::timeout(Duration::from_secs(1), projection_rx.recv())
            .await
            .unwrap()
            .unwrap();

    assert_eq!(event, "ssh://event");
    assert_eq!(payload["event"], "ssh.pty.output");
    assert_eq!(payload["pty_session_id"], pty_session_id.to_string());
    assert_eq!(payload["stream_sequence"], 1);
    handle.shutdown().await.unwrap();
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn first_pty_output_sequence_gap_fails_closed() {
    let port = mock_server(|mut websocket| async move {
        accept_initial_ping(&mut websocket).await;
        websocket
            .send(Message::Text(
                json!({
                    "schema_version": 1,
                    "type": "pty.output",
                    "message_id": Uuid::new_v4(),
                    "causation_id": null,
                    "timestamp": "2026-09-02T00:00:01Z",
                    "payload": {
                        "pty_session_id": Uuid::new_v4(),
                        "data_b64": "b2s=",
                        "stream_sequence": 2
                    }
                })
                .to_string()
                .into(),
            ))
            .await
            .unwrap();
    })
    .await;
    let (projection_tx, _projection_rx) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);
    let connection = RuntimeWebSocketConnection::connect(port, projection_tx)
        .await
        .unwrap();
    let (_handle, task) = connection.split();

    let failure = task.await.unwrap().unwrap_err();

    assert!(matches!(failure, RuntimeWebSocketError::SequenceGap));
}

#[tokio::test]
async fn server_single_owner_rejection_is_not_treated_as_a_retryable_disconnect() {
    let port = mock_server(|mut websocket| async move {
        websocket
            .close(Some(CloseFrame {
                code: CloseCode::Library(4409),
                reason: "runtime WebSocket already has an owner".into(),
            }))
            .await
            .unwrap();
    })
    .await;
    let (projection_tx, _projection_rx) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);

    let failure = match RuntimeWebSocketConnection::connect(port, projection_tx).await {
        Err(error) => error,
        Ok(_) => panic!("second owner connection must be rejected"),
    };

    assert!(matches!(failure, RuntimeWebSocketError::OwnerConflict));
    assert_eq!(failure.error_code(), "RUNTIME_WEBSOCKET_OWNER_CONFLICT");
}

#[tokio::test]
async fn contract_close_after_handshake_preserves_the_server_close_reason() {
    let port = mock_server(|mut websocket| async move {
        accept_initial_ping(&mut websocket).await;
        websocket
            .close(Some(CloseFrame {
                code: CloseCode::Library(4400),
                reason: "runtime WebSocket contract rejected".into(),
            }))
            .await
            .unwrap();
    })
    .await;
    let (projection_tx, _projection_rx) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);
    let connection = RuntimeWebSocketConnection::connect(port, projection_tx)
        .await
        .unwrap();
    let (_handle, task) = connection.split();

    let failure = task.await.unwrap().unwrap_err();

    assert!(matches!(failure, RuntimeWebSocketError::Contract { .. }));
    assert_eq!(failure.error_code(), "RUNTIME_WEBSOCKET_CONTRACT_FAILED");
}

#[test]
fn pty_input_rejects_empty_and_oversized_payloads() {
    let pty_session_id = Uuid::new_v4();
    assert!(PtyInput::new(pty_session_id, b"").is_err());
    assert!(PtyInput::new(pty_session_id, &vec![0; 32_769]).is_err());
    assert_eq!(WEBSOCKET_QUEUE_CAPACITY, 64);
}
