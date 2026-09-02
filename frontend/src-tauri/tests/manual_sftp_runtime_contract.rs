use std::sync::{Arc, Mutex};

use bytes::Bytes;
use harness_shell_lib::{
    runtime::{
        PtyInput, PtyInputResult, RuntimeBinaryHttpRequest, RuntimeClient, RuntimeClientError,
        RuntimeHttpRequest, RuntimeRequestBody,
    },
    sftp::protocol::ManualSftpRuntimeClient,
};
use reqwest::header::{HeaderMap, HeaderValue};
use serde_json::{json, Value};
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq)]
struct RecordedRequest {
    method: String,
    path: String,
    body: Vec<u8>,
    content_type: Option<&'static str>,
    offset: Option<u64>,
}

#[derive(Clone)]
struct MockHttpRuntime {
    recorded: Arc<Mutex<Vec<RecordedRequest>>>,
    json_response: Arc<Mutex<Value>>,
    binary_response: Arc<Mutex<(HeaderMap, Bytes)>>,
}

impl MockHttpRuntime {
    fn new(json_response: Value, headers: HeaderMap, bytes: Bytes) -> Self {
        Self {
            recorded: Arc::new(Mutex::new(Vec::new())),
            json_response: Arc::new(Mutex::new(json_response)),
            binary_response: Arc::new(Mutex::new((headers, bytes))),
        }
    }

    fn take_request(&self) -> RecordedRequest {
        self.recorded.lock().unwrap().remove(0)
    }

    fn record<R>(&self, request: &R) -> Result<(), RuntimeClientError>
    where
        R: RuntimeHttpRequest,
    {
        let request_body = request.body()?;
        let (body, content_type, offset) = match &request_body {
            RuntimeRequestBody::Empty => (Vec::new(), None, None),
            RuntimeRequestBody::Json(bytes) => (bytes.clone(), Some("application/json"), None),
            RuntimeRequestBody::Binary { bytes, offset } => (
                bytes.clone(),
                Some("application/octet-stream"),
                Some(*offset),
            ),
        };
        self.recorded.lock().unwrap().push(RecordedRequest {
            method: request.method().to_string(),
            path: request.path(),
            body,
            content_type,
            offset,
        });
        Ok(())
    }
}

impl RuntimeClient for MockHttpRuntime {
    async fn execute<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeHttpRequest,
    {
        self.record(&request)?;
        let request_id = Uuid::new_v4();
        let mut response = self.json_response.lock().unwrap().clone();
        response["request_id"] = json!(request_id);
        serde_json::from_value(response).map_err(|_| RuntimeClientError::HttpContract {
            reason: "invalid mock response",
        })
    }

    async fn execute_binary<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeBinaryHttpRequest,
    {
        let (headers, bytes) = self.binary_response.lock().unwrap().clone();
        request.decode_success(Uuid::new_v4(), &headers, bytes)
    }

    async fn send_pty_input(
        &self,
        _request: PtyInput,
    ) -> Result<PtyInputResult, RuntimeClientError> {
        Err(RuntimeClientError::Configuration)
    }
}

fn download_headers(sequence: &str, offset: &str, byte_count: &str, eof: &str) -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert("X-Chunk-Sequence", HeaderValue::from_str(sequence).unwrap());
    headers.insert("X-Chunk-Offset", HeaderValue::from_str(offset).unwrap());
    headers.insert(
        "X-Chunk-Byte-Count",
        HeaderValue::from_str(byte_count).unwrap(),
    );
    headers.insert("X-Chunk-EOF", HeaderValue::from_str(eof).unwrap());
    headers
}

#[tokio::test]
async fn upload_chunk_uses_octet_stream_without_base64_expansion() {
    let operation_id = Uuid::new_v4();
    let bytes = vec![0_u8, 1, 2, 255];
    let runtime = MockHttpRuntime::new(
        json!({
            "operation_id": operation_id,
            "sequence": 1,
            "offset": 0,
            "accepted_bytes": bytes.len()
        }),
        HeaderMap::new(),
        Bytes::new(),
    );
    let client = ManualSftpRuntimeClient::new(runtime.clone());

    let ack = client
        .upload_chunk(operation_id, 1, 0, &bytes)
        .await
        .unwrap();

    assert_eq!(ack.next_sequence, 2);
    assert_eq!(ack.next_offset, bytes.len() as u64);
    let request = runtime.take_request();
    assert_eq!(request.method, "PUT");
    assert_eq!(
        request.path,
        format!("/v1/sftp/uploads/{operation_id}/chunks/1")
    );
    assert_eq!(request.content_type, Some("application/octet-stream"));
    assert_eq!(request.offset, Some(0));
    assert_eq!(request.body, bytes);
}

#[tokio::test]
async fn download_chunk_accepts_only_matching_identity_headers() {
    let operation_id = Uuid::new_v4();
    let runtime = MockHttpRuntime::new(
        Value::Null,
        download_headers("2", "6", "3", "false"),
        Bytes::from_static(&[7, 8, 9]),
    );
    let client = ManualSftpRuntimeClient::new(runtime);

    let chunk = client.download_chunk(operation_id, 2, 6).await.unwrap();

    assert_eq!(chunk.operation_id, operation_id);
    assert_eq!(chunk.bytes.as_ref(), &[7, 8, 9]);
    assert_eq!(chunk.next_offset, 9);
    assert!(!chunk.eof);
}

#[tokio::test]
async fn download_chunk_header_mismatch_fails_before_bytes_are_exposed() {
    let operation_id = Uuid::new_v4();
    let runtime = MockHttpRuntime::new(
        Value::Null,
        download_headers("3", "6", "3", "false"),
        Bytes::from_static(&[7, 8, 9]),
    );
    let client = ManualSftpRuntimeClient::new(runtime);

    let error = client.download_chunk(operation_id, 2, 6).await.unwrap_err();

    assert_eq!(error.code(), "RUNTIME_HTTP_CONTRACT_FAILED");
}
