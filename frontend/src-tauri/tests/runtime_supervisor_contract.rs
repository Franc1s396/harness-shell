use std::time::Duration;

use harness_shell_lib::runtime::{
    process::{reserve_loopback_port, sidecar_args},
    status::{RuntimeState, RuntimeSupervisorPhase},
    supervisor::{RuntimeBackend, RuntimeBackendError, RuntimeFailure, RuntimeSupervisor},
};

#[derive(Default)]
struct MockBackend {
    calls: Vec<&'static str>,
    fail_at: Option<&'static str>,
    exits_during_grace: bool,
    killed: bool,
    waited: Option<Duration>,
}

impl MockBackend {
    fn fail_at(stage: &'static str) -> Self {
        Self {
            fail_at: Some(stage),
            ..Self::default()
        }
    }

    fn record(&mut self, stage: &'static str) -> Result<(), RuntimeBackendError> {
        self.calls.push(stage);
        if self.fail_at == Some(stage) {
            return Err(RuntimeBackendError::new(match stage {
                "initialize" => "AUDIT_CHAIN_INVALID",
                "shutdown" => "RUNTIME_SHUTDOWN_BLOCKED",
                _ => "RUNTIME_BACKEND_STAGE_FAILED",
            }));
        }
        Ok(())
    }
}

impl RuntimeBackend for MockBackend {
    async fn start_process(&mut self) -> Result<(), RuntimeBackendError> {
        self.record("start_process")
    }

    async fn wait_http_live(&mut self) -> Result<(), RuntimeBackendError> {
        self.record("http_live")
    }

    async fn initialize(&mut self) -> Result<(), RuntimeBackendError> {
        self.record("initialize")
    }

    async fn wait_http_ready(&mut self) -> Result<(), RuntimeBackendError> {
        self.record("http_ready")
    }

    async fn connect_websocket(&mut self) -> Result<(), RuntimeBackendError> {
        self.record("websocket")
    }

    async fn request_shutdown(&mut self) -> Result<(), RuntimeBackendError> {
        self.record("shutdown")
    }

    async fn wait_for_exit(&mut self, grace: Duration) -> Result<bool, RuntimeBackendError> {
        self.calls.push("wait_for_exit");
        self.waited = Some(grace);
        Ok(self.exits_during_grace)
    }

    fn kill_process_tree(&mut self) -> Result<(), RuntimeBackendError> {
        self.calls.push("kill_process_tree");
        self.killed = true;
        Ok(())
    }
}

#[tokio::test]
async fn ready_is_published_only_after_http_initialize_and_ws_handshake() {
    let supervisor = RuntimeSupervisor::start(MockBackend::default()).await;

    assert_eq!(
        supervisor.internal_transitions(),
        [
            RuntimeSupervisorPhase::StartingProcess,
            RuntimeSupervisorPhase::HttpLive,
            RuntimeSupervisorPhase::Initializing,
            RuntimeSupervisorPhase::HttpReady,
            RuntimeSupervisorPhase::WebSocketConnected,
            RuntimeSupervisorPhase::Ready,
        ]
    );
    assert_eq!(supervisor.public_status().state, RuntimeState::Ready);
    assert_eq!(
        supervisor.backend().calls,
        [
            "start_process",
            "http_live",
            "initialize",
            "http_ready",
            "websocket",
        ]
    );
}

#[tokio::test]
async fn initialization_failure_preserves_first_error_and_never_reaches_ready() {
    let mut supervisor = RuntimeSupervisor::start(MockBackend::fail_at("initialize")).await;

    assert_eq!(supervisor.public_status().state, RuntimeState::Failed);
    assert_eq!(
        supervisor.public_status().error_code.as_deref(),
        Some("AUDIT_CHAIN_INVALID")
    );
    assert!(supervisor.backend().killed);
    assert_eq!(
        supervisor.backend().calls,
        [
            "start_process",
            "http_live",
            "initialize",
            "kill_process_tree"
        ]
    );

    supervisor.fail_runtime(RuntimeFailure::ChildExited);
    assert_eq!(
        supervisor.public_status().error_code.as_deref(),
        Some("AUDIT_CHAIN_INVALID")
    );
    assert_eq!(
        supervisor
            .backend()
            .calls
            .iter()
            .filter(|call| **call == "start_process")
            .count(),
        1
    );
}

#[tokio::test]
async fn websocket_contract_failure_fails_closed_without_respawn() {
    let mut supervisor = RuntimeSupervisor::start(MockBackend::default()).await;

    supervisor.fail_runtime(RuntimeFailure::WebSocketContract);

    assert_eq!(supervisor.public_status().state, RuntimeState::Failed);
    assert_eq!(
        supervisor.public_status().error_code.as_deref(),
        Some("RUNTIME_WEBSOCKET_CONTRACT_FAILED")
    );
    assert_eq!(
        supervisor
            .backend()
            .calls
            .iter()
            .filter(|call| **call == "start_process")
            .count(),
        1
    );
}

#[tokio::test]
async fn shutdown_waits_three_seconds_then_kills_the_owned_process_tree() {
    let mut supervisor = RuntimeSupervisor::start(MockBackend::default()).await;

    supervisor.shutdown().await.unwrap();

    assert_eq!(supervisor.public_status().state, RuntimeState::Stopped);
    assert_eq!(supervisor.backend().waited, Some(Duration::from_secs(3)));
    assert!(supervisor.backend().killed);
    assert!(supervisor.backend().calls.ends_with(&[
        "shutdown",
        "wait_for_exit",
        "kill_process_tree",
    ]));
}

#[tokio::test]
async fn shutdown_endpoint_failure_is_returned_and_never_published_as_stopped() {
    let mut supervisor = RuntimeSupervisor::start(MockBackend::fail_at("shutdown")).await;

    let error = supervisor.shutdown().await.unwrap_err();

    assert_eq!(error.error_code(), "RUNTIME_SHUTDOWN_BLOCKED");
    assert_eq!(supervisor.public_status().state, RuntimeState::Failed);
    assert_eq!(
        supervisor.public_status().error_code.as_deref(),
        Some("RUNTIME_SHUTDOWN_BLOCKED")
    );
}

#[test]
fn dynamic_port_is_released_before_sidecar_spawn_and_args_are_fixed() {
    let port = reserve_loopback_port().unwrap();
    assert_ne!(port, 0);
    let rebound = std::net::TcpListener::bind(("127.0.0.1", port))
        .expect("reserved port must be released before process spawn");
    drop(rebound);
    assert_eq!(
        sidecar_args(port),
        vec!["serve".to_owned(), "--port".to_owned(), port.to_string()]
    );
}
