use std::collections::BTreeMap;

use harness_shell_lib::runtime::{RuntimeClientHandle, TypedHttpClient};
use serde_json::{json, Map, Value};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::{mpsc, oneshot},
};
use uuid::Uuid;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HttpTestResponseKind {
    Response,
    Error,
}

#[derive(Debug)]
pub struct HttpTestResponse {
    pub kind: HttpTestResponseKind,
    pub request_id: Uuid,
    pub payload: Map<String, Value>,
}

#[derive(Debug)]
pub struct TestRuntimeRequest {
    pub payload: Map<String, Value>,
}

pub enum HttpTestCommand {
    Request {
        request_id: Uuid,
        request: TestRuntimeRequest,
        reply: oneshot::Sender<Result<HttpTestResponse, ()>>,
    },
    #[allow(dead_code)]
    Event,
}

#[derive(Debug)]
struct HttpRequest {
    method: String,
    path: String,
    headers: BTreeMap<String, String>,
    body: Vec<u8>,
}

/// Keep the exhaustive coordinator actor/journal fixtures while driving the
/// real typed loopback HTTP routes. The operation projection is test-local.
pub fn runtime_http_test_channel() -> (RuntimeClientHandle, mpsc::Receiver<HttpTestCommand>) {
    let std_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    std_listener.set_nonblocking(true).unwrap();
    let port = std_listener.local_addr().unwrap().port();
    let listener = TcpListener::from_std(std_listener).unwrap();
    let (commands_tx, commands_rx) = mpsc::channel(32);
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let commands_tx = commands_tx.clone();
            tokio::spawn(async move {
                serve_http_request(stream, commands_tx).await;
            });
        }
    });
    let http = TypedHttpClient::new(port).unwrap();
    (RuntimeClientHandle::new_http_only(http), commands_rx)
}

async fn serve_http_request(mut stream: TcpStream, commands: mpsc::Sender<HttpTestCommand>) {
    let Some(request) = read_http_request(&mut stream).await else {
        return;
    };
    let Some((payload, status, binary_download, upload_identity)) = project(&request) else {
        return;
    };
    let request_id = request
        .headers
        .get("x-request-id")
        .and_then(|value| Uuid::parse_str(value).ok())
        .unwrap();
    let (reply_tx, reply_rx) = oneshot::channel();
    if commands
        .send(HttpTestCommand::Request {
            request_id,
            request: TestRuntimeRequest { payload },
            reply: reply_tx,
        })
        .await
        .is_err()
    {
        return;
    }
    let Ok(Ok(frame)) = reply_rx.await else {
        return;
    };
    debug_assert_eq!(frame.request_id, request_id);
    let response = match frame.kind {
        HttpTestResponseKind::Error => problem_response(request_id, &frame.payload),
        HttpTestResponseKind::Response if binary_download => {
            download_response(request_id, &frame.payload)
        }
        HttpTestResponseKind::Response => {
            json_response(request_id, status, frame.payload, upload_identity)
        }
    };
    let _ = stream.write_all(&response).await;
    let _ = stream.shutdown().await;
}

async fn read_http_request(stream: &mut TcpStream) -> Option<HttpRequest> {
    let mut received = Vec::new();
    let header_end = loop {
        let mut chunk = [0_u8; 4096];
        let read = stream.read(&mut chunk).await.ok()?;
        if read == 0 {
            return None;
        }
        received.extend_from_slice(&chunk[..read]);
        if let Some(offset) = received.windows(4).position(|value| value == b"\r\n\r\n") {
            break offset + 4;
        }
    };
    let header_text = std::str::from_utf8(&received[..header_end]).ok()?;
    let mut lines = header_text.split("\r\n");
    let mut request_line = lines.next()?.split_ascii_whitespace();
    let method = request_line.next()?.to_owned();
    let path = request_line.next()?.to_owned();
    let mut headers = BTreeMap::new();
    for line in lines.filter(|line| !line.is_empty()) {
        let (name, value) = line.split_once(':')?;
        headers.insert(name.to_ascii_lowercase(), value.trim().to_owned());
    }
    let content_length = headers
        .get("content-length")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(0);
    while received.len().saturating_sub(header_end) < content_length {
        let mut chunk = [0_u8; 4096];
        let read = stream.read(&mut chunk).await.ok()?;
        if read == 0 {
            return None;
        }
        received.extend_from_slice(&chunk[..read]);
    }
    Some(HttpRequest {
        method,
        path,
        headers,
        body: received[header_end..header_end + content_length].to_vec(),
    })
}

type UploadIdentity = (Uuid, u32, u64, usize);

fn project(
    request: &HttpRequest,
) -> Option<(
    Map<String, Value>,
    &'static str,
    bool,
    Option<UploadIdentity>,
)> {
    let (path, query) = request.path.split_once('?').unwrap_or((&request.path, ""));
    let segments: Vec<_> = path.trim_matches('/').split('/').collect();
    let mut params = if request.body.is_empty()
        || request.headers.get("content-type").map(String::as_str) != Some("application/json")
    {
        Map::new()
    } else {
        serde_json::from_slice::<Value>(&request.body)
            .ok()?
            .as_object()?
            .clone()
    };
    let mut status = "200 OK";
    let mut binary_download = false;
    let mut upload_identity = None;
    let operation = match (request.method.as_str(), segments.as_slice()) {
        ("POST", ["v1", "sftp", "contexts"]) => {
            status = "201 Created";
            "open_context"
        }
        ("POST", ["v1", "sftp", "listings"]) => {
            status = "201 Created";
            "list_begin"
        }
        ("GET", ["v1", "sftp", "listings", id, "batches", sequence]) => {
            params = object(json!({"listing_id": id, "sequence": sequence.parse::<u32>().ok()?}));
            "list_next"
        }
        ("DELETE", ["v1", "sftp", "listings", id]) => {
            params = object(json!({"listing_id": id}));
            "list_close"
        }
        ("POST", ["v1", "sftp", "metadata", "lstat"]) => "lstat",
        ("POST", ["v1", "sftp", "metadata", "readlink"]) => "readlink",
        ("POST", ["v1", "sftp", "metadata", "realpath"]) => "realpath",
        ("POST", ["v1", "sftp", "hashes", "sha256"]) => "sha256",
        ("POST", ["v1", "sftp", "uploads", "preflight"]) => "upload_preflight",
        ("POST", ["v1", "sftp", "uploads"]) => {
            status = "201 Created";
            "upload_begin"
        }
        ("PUT", ["v1", "sftp", "uploads", operation, "chunks", sequence]) => {
            let operation_id = Uuid::parse_str(operation).ok()?;
            let sequence = sequence.parse::<u32>().ok()?;
            let offset = request.headers.get("x-chunk-offset")?.parse::<u64>().ok()?;
            params = object(json!({
                "operation_id": operation_id,
                "sequence": sequence,
                "offset": offset,
                "chunk_bytes": &request.body
            }));
            upload_identity = Some((operation_id, sequence, offset, request.body.len()));
            "upload_chunk"
        }
        ("POST", ["v1", "sftp", "uploads", operation, "finish"]) => {
            params = object(json!({"operation_id": operation}));
            "upload_finish"
        }
        ("POST", ["v1", "sftp", "uploads", operation, "abort"]) => {
            params = object(json!({"operation_id": operation}));
            "upload_abort"
        }
        ("POST", ["v1", "sftp", "downloads"]) => {
            status = "201 Created";
            "download_begin"
        }
        ("GET", ["v1", "sftp", "downloads", operation, "chunks", sequence]) => {
            let offset = query.strip_prefix("offset=")?.parse::<u64>().ok()?;
            params = object(json!({
                "operation_id": operation,
                "sequence": sequence.parse::<u32>().ok()?,
                "offset": offset
            }));
            binary_download = true;
            "download_chunk"
        }
        ("POST", ["v1", "sftp", "downloads", operation, "finish"]) => {
            params = object(json!({"operation_id": operation}));
            "download_finish"
        }
        ("POST", ["v1", "sftp", "downloads", operation, "abort"]) => {
            params = object(json!({"operation_id": operation}));
            "download_abort"
        }
        ("POST", ["v1", "sftp", "directories"]) => {
            status = "201 Created";
            "mkdir"
        }
        ("POST", ["v1", "sftp", "renames"]) => "rename",
        ("POST", ["v1", "sftp", "removals"]) => "remove",
        ("POST", ["v1", "sftp", "deletions", "preflight"]) => "delete_preflight",
        ("POST", ["v1", "sftp", "deletions", operation, "execute"]) => {
            params = object(json!({"delete_plan_id": operation}));
            "delete_execute"
        }
        ("GET", ["v1", "sftp", "recoveries"]) => {
            params = object(json!({"recovery_id": query.strip_prefix("recovery_id=")?}));
            "recovery_inspect"
        }
        ("POST", ["v1", "sftp", "recoveries", recovery, "actions"]) => {
            params.insert("recovery_id".into(), Value::String((*recovery).to_owned()));
            "recovery_execute"
        }
        _ => return None,
    };
    Some((
        object(json!({"operation": operation, "params": params})),
        status,
        binary_download,
        upload_identity,
    ))
}

fn json_response(
    request_id: Uuid,
    status: &str,
    mut payload: Map<String, Value>,
    upload: Option<UploadIdentity>,
) -> Vec<u8> {
    if let Some((operation_id, sequence, offset, accepted_bytes)) = upload {
        let chunk = payload.get("chunk").and_then(Value::as_object);
        let next_sequence = chunk
            .and_then(|value| value.get("next_sequence"))
            .and_then(Value::as_u64)
            .unwrap_or(sequence as u64 + 1);
        let next_offset = chunk
            .and_then(|value| value.get("next_offset"))
            .and_then(Value::as_u64)
            .unwrap_or(offset + accepted_bytes as u64);
        payload = object(json!({
            "operation_id": operation_id,
            "sequence": if next_sequence == sequence as u64 + 1 { sequence as u64 } else { next_sequence },
            "offset": offset,
            "accepted_bytes": next_offset.saturating_sub(offset)
        }));
    }
    payload.insert("request_id".into(), json!(request_id));
    let body = serde_json::to_vec(&payload).unwrap();
    wire_response(status, "application/json", request_id, &[], body)
}

fn problem_response(request_id: Uuid, payload: &Map<String, Value>) -> Vec<u8> {
    let error_code = payload
        .get("error_code")
        .and_then(Value::as_str)
        .unwrap_or("SIDECAR_REQUEST_FAILED");
    let mut details = Map::new();
    if let Some(state) = payload.get("operation_state") {
        details.insert("operation_state".into(), state.clone());
    }
    let body = serde_json::to_vec(&json!({
        "type": "urn:harness-shell:error:manual-sftp",
        "title": "Manual SFTP request failed",
        "status": 422,
        "error_code": error_code,
        "message": "Manual SFTP request failed",
        "request_id": request_id,
        "details": details
    }))
    .unwrap();
    wire_response(
        "422 Unprocessable Entity",
        "application/problem+json",
        request_id,
        &[],
        body,
    )
}

fn download_response(request_id: Uuid, payload: &Map<String, Value>) -> Vec<u8> {
    let chunk = payload.get("chunk").and_then(Value::as_object).unwrap();
    let sequence = chunk.get("sequence").and_then(Value::as_u64).unwrap();
    let offset = chunk.get("offset").and_then(Value::as_u64).unwrap();
    let eof = chunk.get("eof").and_then(Value::as_bool).unwrap();
    let next_offset = chunk.get("next_offset").and_then(Value::as_u64).unwrap();
    let bytes = test_bytes(chunk.get("chunk_bytes").unwrap());
    let headers = [
        ("X-Chunk-Sequence", sequence.to_string()),
        ("X-Chunk-Offset", offset.to_string()),
        (
            "X-Chunk-Byte-Count",
            next_offset.saturating_sub(offset).to_string(),
        ),
        ("X-Chunk-EOF", if eof { "true" } else { "false" }.to_owned()),
    ];
    wire_response(
        "200 OK",
        "application/octet-stream",
        request_id,
        &headers,
        bytes,
    )
}

fn wire_response(
    status: &str,
    content_type: &str,
    request_id: Uuid,
    extra_headers: &[(&str, String)],
    body: Vec<u8>,
) -> Vec<u8> {
    let mut head = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nX-Request-ID: {request_id}\r\n"
    );
    for (name, value) in extra_headers {
        head.push_str(name);
        head.push_str(": ");
        head.push_str(value);
        head.push_str("\r\n");
    }
    head.push_str(&format!(
        "Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    ));
    head.into_bytes().into_iter().chain(body).collect()
}

fn object(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

pub fn test_bytes(value: &Value) -> Vec<u8> {
    value
        .as_array()
        .expect("test byte payload must be an array")
        .iter()
        .map(|byte| {
            byte.as_u64()
                .and_then(|byte| u8::try_from(byte).ok())
                .expect("test byte payload must contain only u8 values")
        })
        .collect()
}
