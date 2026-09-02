//! Strict loopback HTTP/WebSocket runtime boundary.

pub mod client;
#[cfg(target_os = "windows")]
pub mod desktop;
pub mod http;
#[cfg(target_os = "windows")]
pub mod job;
pub mod models;
pub mod process;
pub mod status;
pub mod supervisor;
pub mod websocket;

pub use client::{RuntimeClient, RuntimeClientError, RuntimeClientHandle};
pub use http::{
    CancelRequest, ClosePtyRequest, ConfirmHostKeyRequest, ConnectSshRequest,
    CreateAgentApiConfigRequest, CreateConnectionRequest, DeleteAgentApiConfigRequest,
    DeleteConnectionRequest, DisconnectSshRequest, GetAgentApiConfigRequest, GetConnectionRequest,
    HealthLiveRequest, HealthReadyRequest, InitializeRuntimeRequest, InspectHostKeyRequest,
    ListAgentApiConfigsRequest, ListConnectionsRequest, OpenPtyRequest, ReplaceHostKeyRequest,
    ResizePtyRequest, RunAgentTurnRequest, RuntimeBinaryHttpRequest, RuntimeHttpRequest,
    RuntimeRequestBody, RuntimeStateRequest, ShutdownRuntimeRequest, SshJumpRequest,
    TypedHttpClient, UpdateAgentApiConfigRequest, UpdateConnectionRequest,
};
pub use status::{RuntimeState, RuntimeStatus};
pub use websocket::{PtyInput, PtyInputResult};
