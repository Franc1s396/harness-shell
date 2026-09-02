use std::sync::Arc;

use tokio::sync::RwLock;

use super::{
    http::{RuntimeBinaryHttpRequest, RuntimeHttpRequest},
    models::ProblemDetails,
    websocket::{PtyInput, PtyInputResult, RuntimeWebSocketHandle},
    TypedHttpClient,
};

/// Stable Core-owned handle that Tauri commands and coordinators will use after cutover.
#[derive(Clone)]
pub struct RuntimeClientHandle {
    connected: Arc<RwLock<Option<ConnectedRuntime>>>,
}

/// Typed command-facing seam. Implementations can only accept sealed HTTP
/// requests or the dedicated PTY WebSocket input contract.
#[allow(async_fn_in_trait)]
pub trait RuntimeClient: Send + Sync {
    async fn execute<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeHttpRequest;

    async fn execute_binary<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeBinaryHttpRequest,
    {
        let _ = request;
        Err(RuntimeClientError::Configuration)
    }

    async fn send_pty_input(&self, request: PtyInput)
        -> Result<PtyInputResult, RuntimeClientError>;
}

#[derive(Clone)]
struct ConnectedRuntime {
    http: TypedHttpClient,
    websocket: Option<RuntimeWebSocketHandle>,
}

impl RuntimeClientHandle {
    pub fn pending() -> Self {
        Self {
            connected: Arc::new(RwLock::new(None)),
        }
    }

    pub fn new(http: TypedHttpClient, websocket: RuntimeWebSocketHandle) -> Self {
        Self {
            connected: Arc::new(RwLock::new(Some(ConnectedRuntime {
                http,
                websocket: Some(websocket),
            }))),
        }
    }

    /// Builds an HTTP-only handle for contract-test servers. Production
    /// supervisors publish HTTP and WebSocket together through `publish`.
    #[doc(hidden)]
    pub fn new_http_only(http: TypedHttpClient) -> Self {
        Self {
            connected: Arc::new(RwLock::new(Some(ConnectedRuntime {
                http,
                websocket: None,
            }))),
        }
    }

    pub async fn publish(
        &self,
        http: TypedHttpClient,
        websocket: RuntimeWebSocketHandle,
    ) -> Result<(), RuntimeClientError> {
        let mut connected = self.connected.write().await;
        if connected.is_some() {
            return Err(RuntimeClientError::AlreadyPublished);
        }
        *connected = Some(ConnectedRuntime {
            http,
            websocket: Some(websocket),
        });
        Ok(())
    }

    /// Revoke the published transport before exposing a terminal runtime state.
    /// Cloned command/coordinator handles observe the same shared fail-closed slot.
    pub async fn revoke(&self) {
        *self.connected.write().await = None;
    }

    pub async fn execute<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeHttpRequest,
    {
        let http = self
            .connected
            .read()
            .await
            .as_ref()
            .map(|runtime| runtime.http.clone())
            .ok_or(RuntimeClientError::NotReady)?;
        http.execute(request).await
    }

    pub async fn execute_binary<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeBinaryHttpRequest,
    {
        let http = self
            .connected
            .read()
            .await
            .as_ref()
            .map(|runtime| runtime.http.clone())
            .ok_or(RuntimeClientError::NotReady)?;
        http.execute_binary(request).await
    }

    pub async fn send_pty_input(
        &self,
        request: PtyInput,
    ) -> Result<PtyInputResult, RuntimeClientError> {
        let websocket = self
            .connected
            .read()
            .await
            .as_ref()
            .and_then(|runtime| runtime.websocket.clone())
            .ok_or(RuntimeClientError::NotReady)?;
        websocket.send_pty_input(request).await
    }

    pub async fn shutdown_websocket(&self) -> Result<(), RuntimeClientError> {
        let websocket = self
            .connected
            .read()
            .await
            .as_ref()
            .and_then(|runtime| runtime.websocket.clone())
            .ok_or(RuntimeClientError::NotReady)?;
        websocket.shutdown().await
    }
}

impl RuntimeClient for RuntimeClientHandle {
    async fn execute<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeHttpRequest,
    {
        RuntimeClientHandle::execute(self, request).await
    }

    async fn execute_binary<R>(&self, request: R) -> Result<R::Response, RuntimeClientError>
    where
        R: RuntimeBinaryHttpRequest,
    {
        RuntimeClientHandle::execute_binary(self, request).await
    }

    async fn send_pty_input(
        &self,
        request: PtyInput,
    ) -> Result<PtyInputResult, RuntimeClientError> {
        RuntimeClientHandle::send_pty_input(self, request).await
    }
}

#[derive(Clone, Debug, thiserror::Error)]
pub enum RuntimeClientError {
    #[error("runtime is not ready")]
    NotReady,
    #[error("runtime client was already published")]
    AlreadyPublished,
    #[error("runtime client configuration is invalid")]
    Configuration,
    #[error("runtime HTTP transport failed")]
    HttpTransport,
    #[error("runtime request body exceeds the fixed limit")]
    HttpRequestTooLarge,
    #[error("runtime response body exceeds the fixed limit")]
    HttpResponseTooLarge,
    #[error("runtime HTTP contract failed: {reason}")]
    HttpContract { reason: &'static str },
    #[error("runtime request failed")]
    Problem(ProblemDetails),
    #[error("runtime WebSocket is closed")]
    WebSocketClosed,
    #[error("runtime WebSocket request capacity is exhausted")]
    WebSocketCapacity,
    #[error("runtime WebSocket domain request failed")]
    WebSocketDomain { error_code: String },
}

impl RuntimeClientError {
    pub fn error_code(&self) -> &str {
        match self {
            Self::NotReady => "RUNTIME_NOT_READY",
            Self::AlreadyPublished => "RUNTIME_CLIENT_ALREADY_PUBLISHED",
            Self::Configuration => "RUNTIME_CLIENT_CONFIGURATION_INVALID",
            Self::HttpTransport => "RUNTIME_HTTP_TRANSPORT_FAILED",
            Self::HttpRequestTooLarge => "RUNTIME_HTTP_REQUEST_TOO_LARGE",
            Self::HttpResponseTooLarge => "RUNTIME_HTTP_RESPONSE_TOO_LARGE",
            Self::HttpContract { .. } => "RUNTIME_HTTP_CONTRACT_FAILED",
            Self::Problem(problem) => &problem.error_code,
            Self::WebSocketClosed => "RUNTIME_WEBSOCKET_CLOSED",
            Self::WebSocketCapacity => "REQUEST_CAPACITY_EXCEEDED",
            Self::WebSocketDomain { error_code } => error_code,
        }
    }

    pub const fn problem(&self) -> Option<&ProblemDetails> {
        match self {
            Self::Problem(problem) => Some(problem),
            _ => None,
        }
    }
}
