use std::{collections::BTreeMap, time::Duration};

use harness_shell_lib::runtime::{
    models::HealthLiveResponse, HealthLiveRequest, RuntimeClientError, RuntimeClientHandle,
    RuntimeStateRequest, TypedHttpClient,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::oneshot,
};
use uuid::Uuid;

#[derive(Debug)]
struct RecordedRequest {
    method: String,
    path: String,
    headers: BTreeMap<String, Vec<String>>,
    body: Vec<u8>,
}

async fn serve_once(
    response: impl FnOnce(&RecordedRequest) -> String + Send + 'static,
) -> (u16, oneshot::Receiver<RecordedRequest>) {
    serve_once_after(Duration::ZERO, response).await
}

async fn serve_once_after(
    delay: Duration,
    response: impl FnOnce(&RecordedRequest) -> String + Send + 'static,
) -> (u16, oneshot::Receiver<RecordedRequest>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (record_tx, record_rx) = oneshot::channel();
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut received = Vec::new();
        let header_end = loop {
            let mut chunk = [0_u8; 1024];
            let read = stream.read(&mut chunk).await.unwrap();
            assert!(read > 0, "client closed before request headers");
            received.extend_from_slice(&chunk[..read]);
            if let Some(offset) = received.windows(4).position(|value| value == b"\r\n\r\n") {
                break offset + 4;
            }
        };
        let header_text = std::str::from_utf8(&received[..header_end]).unwrap();
        let mut lines = header_text.split("\r\n");
        let request_line = lines.next().unwrap();
        let mut request_parts = request_line.split_ascii_whitespace();
        let method = request_parts.next().unwrap().to_owned();
        let path = request_parts.next().unwrap().to_owned();
        let mut headers = BTreeMap::<String, Vec<String>>::new();
        for line in lines.filter(|line| !line.is_empty()) {
            let (name, value) = line.split_once(':').unwrap();
            headers
                .entry(name.to_ascii_lowercase())
                .or_default()
                .push(value.trim().to_owned());
        }
        let content_length = headers
            .get("content-length")
            .and_then(|values| values.first())
            .map(|value| value.parse::<usize>().unwrap())
            .unwrap_or(0);
        while received.len() - header_end < content_length {
            let mut chunk = [0_u8; 1024];
            let read = stream.read(&mut chunk).await.unwrap();
            assert!(read > 0, "client closed before request body");
            received.extend_from_slice(&chunk[..read]);
        }
        let request = RecordedRequest {
            method,
            path,
            headers,
            body: received[header_end..header_end + content_length].to_vec(),
        };
        let wire_response = response(&request);
        tokio::time::sleep(delay).await;
        stream.write_all(wire_response.as_bytes()).await.unwrap();
        stream.shutdown().await.unwrap();
        record_tx.send(request).ok();
    });
    (port, record_rx)
}

fn response(status: &str, content_type: &str, request_id: &str, body: &str) -> String {
    format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nX-Request-ID: {request_id}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

#[tokio::test]
async fn typed_client_does_not_impose_a_total_request_deadline() {
    let (port, _) = serve_once_after(Duration::from_millis(75), |request| {
        let request_id = &request.headers["x-request-id"][0];
        response(
            "200 OK",
            "application/json",
            request_id,
            &format!(r#"{{"request_id":"{request_id}","live":true}}"#),
        )
    })
    .await;
    let client = TypedHttpClient::new(port).unwrap();

    let result: HealthLiveResponse = client.execute(HealthLiveRequest).await.unwrap();

    assert!(result.live);
}

#[tokio::test]
async fn typed_client_fixes_method_path_headers_and_decodes_strict_success() {
    let (port, recorded) = serve_once(|request| {
        let request_id = &request.headers["x-request-id"][0];
        response(
            "200 OK",
            "application/json",
            request_id,
            &format!(r#"{{"request_id":"{request_id}","live":true}}"#),
        )
    })
    .await;
    let client = TypedHttpClient::new(port).unwrap();

    let result: HealthLiveResponse = client.execute(HealthLiveRequest).await.unwrap();

    assert!(result.live);
    let request = recorded.await.unwrap();
    assert_eq!(request.method, "GET");
    assert_eq!(request.path, "/v1/health/live");
    assert_eq!(
        request.headers["accept"],
        ["application/json, application/problem+json"]
    );
    assert_eq!(request.headers["x-request-id"].len(), 1);
    assert!(request.body.is_empty());
}

#[tokio::test]
async fn strict_success_rejects_unknown_fields_and_request_id_mismatch() {
    for mutation in ["unknown-field", "request-id-mismatch"] {
        let (port, _) = serve_once(move |request| {
            let request_id = &request.headers["x-request-id"][0];
            let body = match mutation {
                "unknown-field" => {
                    format!(r#"{{"request_id":"{request_id}","state":"READY","unexpected":true}}"#)
                }
                _ => format!(r#"{{"request_id":"{}","state":"READY"}}"#, Uuid::new_v4()),
            };
            response("200 OK", "application/json", request_id, &body)
        })
        .await;
        let client = TypedHttpClient::new(port).unwrap();

        assert!(matches!(
            client.execute(RuntimeStateRequest).await,
            Err(RuntimeClientError::HttpContract { .. })
        ));
    }
}

#[tokio::test]
async fn problem_details_validate_status_request_id_content_type_and_unknown_fields() {
    for mutation in ["status", "request-id", "content-type", "unknown-field"] {
        let (port, _) = serve_once(move |request| {
            let request_id = &request.headers["x-request-id"][0];
            let body_request_id = if mutation == "request-id" {
                Uuid::new_v4().to_string()
            } else {
                request_id.clone()
            };
            let mut body = format!(
                r#"{{"type":"urn:harness-shell:error:runtime-not-ready","title":"Runtime not ready","status":{},"error_code":"RUNTIME_NOT_READY","message":"Runtime is not ready","request_id":"{body_request_id}","details":{{}}"#,
                if mutation == "status" { 409 } else { 503 }
            );
            if mutation == "unknown-field" {
                body.push_str(r#","unexpected":true"#);
            }
            body.push('}');
            response(
                "503 Service Unavailable",
                if mutation == "content-type" {
                    "application/json"
                } else {
                    "application/problem+json"
                },
                request_id,
                &body,
            )
        })
        .await;
        let client = TypedHttpClient::new(port).unwrap();

        assert!(matches!(
            client.execute(RuntimeStateRequest).await,
            Err(RuntimeClientError::HttpContract { .. })
        ));
    }
}

#[tokio::test]
async fn valid_problem_details_preserve_the_stable_domain_error() {
    let (port, _) = serve_once(|request| {
        let request_id = &request.headers["x-request-id"][0];
        let body = format!(
            r#"{{"type":"urn:harness-shell:error:runtime-not-ready","title":"Runtime not ready","status":503,"error_code":"RUNTIME_NOT_READY","message":"Runtime is not ready","request_id":"{request_id}","details":{{"state":"LIVE_NOT_INITIALIZED"}}}}"#
        );
        response(
            "503 Service Unavailable",
            "application/problem+json",
            request_id,
            &body,
        )
    })
    .await;
    let client = TypedHttpClient::new(port).unwrap();

    let error = client.execute(RuntimeStateRequest).await.unwrap_err();

    assert_eq!(error.error_code(), "RUNTIME_NOT_READY");
    assert_eq!(error.problem().unwrap().status, 503);
}

#[tokio::test]
async fn duplicate_response_request_id_header_fails_closed() {
    let (port, _) = serve_once(|request| {
        let request_id = &request.headers["x-request-id"][0];
        let body = format!(r#"{{"request_id":"{request_id}","live":true}}"#);
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Request-ID: {request_id}\r\nX-Request-ID: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            Uuid::new_v4(),
            body.len()
        )
    })
    .await;
    let client = TypedHttpClient::new(port).unwrap();

    assert!(matches!(
        client.execute(HealthLiveRequest).await,
        Err(RuntimeClientError::HttpContract { .. })
    ));
}

#[tokio::test]
async fn managed_runtime_handle_fails_closed_before_supervisor_publication() {
    let runtime = RuntimeClientHandle::pending();

    let error = runtime.execute(HealthLiveRequest).await.unwrap_err();

    assert_eq!(error.error_code(), "RUNTIME_NOT_READY");
}

#[tokio::test]
async fn managed_runtime_handle_fails_closed_after_supervisor_revocation() {
    let (port, _) = serve_once(|request| {
        let request_id = &request.headers["x-request-id"][0];
        let body = format!(r#"{{"request_id":"{request_id}","live":true}}"#);
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Request-ID: {request_id}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    })
    .await;
    let http = TypedHttpClient::new(port).unwrap();
    let runtime = RuntimeClientHandle::new_http_only(http);

    runtime.revoke().await;
    let error = runtime.execute(HealthLiveRequest).await.unwrap_err();

    assert_eq!(error.error_code(), "RUNTIME_NOT_READY");
}
