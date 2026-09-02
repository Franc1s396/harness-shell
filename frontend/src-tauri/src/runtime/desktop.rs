use std::{future::Future, path::Path, time::Duration};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use tauri::{AppHandle, Emitter, Runtime};
use tokio::{
    sync::mpsc::{self, Receiver, UnboundedReceiver},
    task::JoinHandle,
};

use crate::{
    app_state::{RuntimeControl, RuntimeStateHandle},
    vault::RuntimeKeys,
};

use super::{
    models::{
        now_utc, RuntimeInitializeBody, RuntimeModelError, RuntimePhase, HEARTBEAT_INTERVAL_MS,
        HEARTBEAT_TIMEOUT_MS,
    },
    process::{
        reserve_loopback_port, BackendProcessError, BackendProcessEvent, BackendProcessOwner,
    },
    websocket::{
        RuntimeProjection, RuntimeWebSocketConnection, RuntimeWebSocketError,
        RuntimeWebSocketHandle, WEBSOCKET_QUEUE_CAPACITY,
    },
    HealthLiveRequest, HealthReadyRequest, InitializeRuntimeRequest, RuntimeClientError,
    RuntimeClientHandle, RuntimeState, ShutdownRuntimeRequest, TypedHttpClient,
};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
const LIVE_RETRY_DELAY: Duration = Duration::from_millis(25);
const WEBSOCKET_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);
const PROCESS_SHUTDOWN_GRACE: Duration = Duration::from_secs(3);
const MAIN_WINDOW_LABEL: &str = "main";

/// Own the packaged child, typed transports, event projection, and terminal cleanup.
pub async fn supervise_desktop_runtime<R: Runtime>(
    app: AppHandle<R>,
    state: RuntimeStateHandle,
    runtime_client: RuntimeClientHandle,
    mut control: UnboundedReceiver<RuntimeControl>,
    runtime_db_path: &Path,
    extraction_directory: &Path,
    runtime_keys: RuntimeKeys,
) -> Result<(), DesktopRuntimeError> {
    let port = match reserve_loopback_port() {
        Ok(port) => port,
        Err(error) => {
            let failure = DesktopRuntimeError::process(error);
            publish_failure_once(&state, failure.error_code());
            return Err(failure);
        }
    };
    let mut process = match BackendProcessOwner::spawn(&app, extraction_directory, port) {
        Ok(process) => process,
        Err(error) => {
            let failure = DesktopRuntimeError::process(error);
            publish_failure_once(&state, failure.error_code());
            return Err(failure);
        }
    };
    publish_state(&state, RuntimeState::Handshaking, None);

    let startup = start_transport(
        &mut process,
        &mut control,
        port,
        runtime_db_path,
        runtime_keys,
    )
    .await;
    let (http, websocket, websocket_task, projection_rx) = match startup {
        Ok(transport) => transport,
        Err(error) if error.is_shutdown_requested() => {
            runtime_client.revoke().await;
            terminate_if_running(&mut process);
            publish_state(&state, RuntimeState::Stopped, None);
            return Ok(());
        }
        Err(error) => {
            runtime_client.revoke().await;
            terminate_if_running(&mut process);
            publish_failure_once(&state, error.error_code());
            return Err(error);
        }
    };

    if let Err(error) = runtime_client
        .publish(http.clone(), websocket.clone())
        .await
    {
        let failure = DesktopRuntimeError::client(error);
        terminate_if_running(&mut process);
        publish_failure_once(&state, failure.error_code());
        return Err(failure);
    }
    publish_state(&state, RuntimeState::Ready, None);

    run_ready_runtime(
        &app,
        &state,
        &runtime_client,
        &mut control,
        &mut process,
        http,
        websocket,
        websocket_task,
        projection_rx,
    )
    .await
}

type StartedTransport = (
    TypedHttpClient,
    RuntimeWebSocketHandle,
    JoinHandle<Result<(), RuntimeWebSocketError>>,
    Receiver<RuntimeProjection>,
);

async fn start_transport(
    process: &mut BackendProcessOwner,
    control: &mut UnboundedReceiver<RuntimeControl>,
    port: u16,
    runtime_db_path: &Path,
    runtime_keys: RuntimeKeys,
) -> Result<StartedTransport, DesktopRuntimeError> {
    let http = TypedHttpClient::new(port).map_err(DesktopRuntimeError::client)?;
    startup_stage(process, control, wait_until_live(http.clone())).await?;

    let runtime_db_path = runtime_db_path
        .to_str()
        .filter(|_| runtime_db_path.is_absolute())
        .ok_or_else(|| DesktopRuntimeError::new("RUNTIME_DATABASE_PATH_INVALID"))?;
    let initialize = RuntimeInitializeBody::new(
        env!("CARGO_PKG_VERSION"),
        runtime_db_path,
        STANDARD.encode(runtime_keys.runtime_data_key.as_slice()),
        STANDARD.encode(runtime_keys.audit_hmac_key.as_slice()),
        HEARTBEAT_INTERVAL_MS,
        HEARTBEAT_TIMEOUT_MS,
    )
    .map_err(DesktopRuntimeError::model)?;
    let initialized = startup_stage(process, control, async {
        http.execute(InitializeRuntimeRequest(initialize))
            .await
            .map_err(DesktopRuntimeError::client)
    })
    .await?;
    if initialized.state != RuntimePhase::Ready {
        return Err(DesktopRuntimeError::new(
            "RUNTIME_INITIALIZE_CONTRACT_FAILED",
        ));
    }

    let ready = startup_stage(process, control, async {
        http.execute(HealthReadyRequest)
            .await
            .map_err(DesktopRuntimeError::client)
    })
    .await?;
    if !ready.ready || ready.state != RuntimePhase::Ready {
        return Err(DesktopRuntimeError::new("RUNTIME_READY_CONTRACT_FAILED"));
    }

    let (projection_tx, projection_rx) = mpsc::channel(WEBSOCKET_QUEUE_CAPACITY);
    let connection = startup_stage(process, control, async {
        RuntimeWebSocketConnection::connect(port, projection_tx)
            .await
            .map_err(DesktopRuntimeError::websocket)
    })
    .await?;
    let (websocket, websocket_task) = connection.split();
    Ok((http, websocket, websocket_task, projection_rx))
}

async fn wait_until_live(http: TypedHttpClient) -> Result<(), DesktopRuntimeError> {
    loop {
        match http.execute(HealthLiveRequest).await {
            Ok(response) if response.live => return Ok(()),
            Ok(_) => return Err(DesktopRuntimeError::new("RUNTIME_LIVENESS_CONTRACT_FAILED")),
            Err(RuntimeClientError::HttpTransport) => {
                tokio::time::sleep(LIVE_RETRY_DELAY).await;
            }
            Err(error) => return Err(DesktopRuntimeError::client(error)),
        }
    }
}

async fn startup_stage<T, F>(
    process: &mut BackendProcessOwner,
    control: &mut UnboundedReceiver<RuntimeControl>,
    future: F,
) -> Result<T, DesktopRuntimeError>
where
    F: Future<Output = Result<T, DesktopRuntimeError>>,
{
    tokio::select! {
        biased;
        command = control.recv() => match command {
            Some(RuntimeControl::Shutdown) | None => {
                Err(DesktopRuntimeError::new("RUNTIME_STARTUP_SHUTDOWN_REQUESTED"))
            }
        },
        event = process.next_event() => {
            Err(unexpected_process_event(event))
        }
        result = tokio::time::timeout(STARTUP_TIMEOUT, future) => {
            result
                .map_err(|_| DesktopRuntimeError::new("RUNTIME_STARTUP_TIMEOUT"))?
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn run_ready_runtime<R: Runtime>(
    app: &AppHandle<R>,
    state: &RuntimeStateHandle,
    runtime_client: &RuntimeClientHandle,
    control: &mut UnboundedReceiver<RuntimeControl>,
    process: &mut BackendProcessOwner,
    http: TypedHttpClient,
    websocket: RuntimeWebSocketHandle,
    mut websocket_task: JoinHandle<Result<(), RuntimeWebSocketError>>,
    mut projection_rx: Receiver<RuntimeProjection>,
) -> Result<(), DesktopRuntimeError> {
    loop {
        let failure = tokio::select! {
            biased;
            command = control.recv() => match command {
                Some(RuntimeControl::Shutdown) | None => {
                    runtime_client.revoke().await;
                    return finish_shutdown(
                        state,
                        process,
                        &http,
                        &websocket,
                        &mut websocket_task,
                    ).await;
                }
            },
            event = process.next_event() => unexpected_process_event(event),
            result = &mut websocket_task => websocket_task_failure(result),
            projection = projection_rx.recv() => match projection {
                Some(projection) => {
                    if app
                        .emit_to(MAIN_WINDOW_LABEL, projection.event, projection.payload)
                        .is_ok()
                    {
                        continue;
                    }
                    DesktopRuntimeError::new("RUNTIME_EVENT_PROJECTION_FAILED")
                }
                None => DesktopRuntimeError::new("RUNTIME_EVENT_PROJECTION_CLOSED"),
            },
        };

        runtime_client.revoke().await;
        // Preserve the triggering failure while still giving the owned backend
        // its one bounded graceful-shutdown opportunity before Job termination.
        cleanup_failed_runtime(process, &http, &websocket).await;
        publish_failure_once(state, failure.error_code());
        return Err(failure);
    }
}

async fn cleanup_failed_runtime(
    process: &mut BackendProcessOwner,
    http: &TypedHttpClient,
    websocket: &RuntimeWebSocketHandle,
) {
    if process.pid().is_none() {
        return;
    }

    let _ = websocket.shutdown().await;
    let _ = http.execute(ShutdownRuntimeRequest).await;
    let exited = matches!(
        tokio::time::timeout(PROCESS_SHUTDOWN_GRACE, process.next_event()).await,
        Ok(Ok(BackendProcessEvent::Terminated { code: Some(0) }))
    );
    if !exited {
        terminate_if_running(process);
    }
}

async fn finish_shutdown(
    state: &RuntimeStateHandle,
    process: &mut BackendProcessOwner,
    http: &TypedHttpClient,
    websocket: &RuntimeWebSocketHandle,
    websocket_task: &mut JoinHandle<Result<(), RuntimeWebSocketError>>,
) -> Result<(), DesktopRuntimeError> {
    let mut first_error = None;
    if websocket.shutdown().await.is_err() {
        first_error = Some(DesktopRuntimeError::new(
            "RUNTIME_WEBSOCKET_SHUTDOWN_FAILED",
        ));
    } else {
        match tokio::time::timeout(WEBSOCKET_SHUTDOWN_TIMEOUT, websocket_task).await {
            Ok(Ok(Ok(()))) => {}
            Ok(Ok(Err(error))) => first_error = Some(DesktopRuntimeError::websocket(error)),
            Ok(Err(_)) => {
                first_error = Some(DesktopRuntimeError::new("RUNTIME_WEBSOCKET_TASK_FAILED"))
            }
            Err(_) => {
                first_error = Some(DesktopRuntimeError::new(
                    "RUNTIME_WEBSOCKET_SHUTDOWN_TIMEOUT",
                ))
            }
        }
    }

    let shutdown_result = http.execute(ShutdownRuntimeRequest).await;
    match shutdown_result {
        Ok(response) if response.state == RuntimePhase::Stopped => {}
        Ok(_) => {
            first_error.get_or_insert_with(|| {
                DesktopRuntimeError::new("RUNTIME_SHUTDOWN_CONTRACT_FAILED")
            });
        }
        Err(error) => {
            first_error.get_or_insert_with(|| DesktopRuntimeError::client(error));
        }
    };

    let exited_cleanly = match tokio::time::timeout(PROCESS_SHUTDOWN_GRACE, process.next_event())
        .await
    {
        Ok(Ok(BackendProcessEvent::Terminated { code: Some(0) })) => true,
        Ok(Ok(BackendProcessEvent::Terminated { .. })) => {
            first_error
                .get_or_insert_with(|| DesktopRuntimeError::new("SIDECAR_SHUTDOWN_EXIT_FAILED"));
            false
        }
        Ok(Err(error)) => {
            first_error.get_or_insert_with(|| DesktopRuntimeError::process(error));
            false
        }
        Err(_) => {
            first_error.get_or_insert_with(|| DesktopRuntimeError::new("SIDECAR_SHUTDOWN_TIMEOUT"));
            false
        }
    };
    if !exited_cleanly {
        terminate_if_running(process);
    }

    if let Some(error) = first_error {
        publish_failure_once(state, error.error_code());
        return Err(error);
    }
    publish_state(state, RuntimeState::Stopped, None);
    Ok(())
}

fn unexpected_process_event(
    event: Result<BackendProcessEvent, BackendProcessError>,
) -> DesktopRuntimeError {
    match event {
        Ok(BackendProcessEvent::Terminated { .. }) => DesktopRuntimeError::new("SIDECAR_EXITED"),
        Err(error) => DesktopRuntimeError::process(error),
    }
}

fn websocket_task_failure(
    result: Result<Result<(), RuntimeWebSocketError>, tokio::task::JoinError>,
) -> DesktopRuntimeError {
    match result {
        Ok(Ok(())) => DesktopRuntimeError::new("RUNTIME_WEBSOCKET_DISCONNECTED"),
        Ok(Err(error)) => DesktopRuntimeError::websocket(error),
        Err(_) => DesktopRuntimeError::new("RUNTIME_WEBSOCKET_TASK_FAILED"),
    }
}

fn terminate_if_running(process: &mut BackendProcessOwner) {
    if process.pid().is_some() {
        if let Err(error) = process.kill() {
            log::error!(
                target: "harness_shell::runtime",
                "packaged backend process-tree cleanup failed: {error}"
            );
        }
    }
}

fn publish_state(
    state: &RuntimeStateHandle,
    runtime_state: RuntimeState,
    error_code: Option<String>,
) {
    let mut status = state.status();
    status.state = runtime_state;
    status.error_code = error_code;
    status.recoverable = false;
    status.last_heartbeat_at = if status.state == RuntimeState::Ready {
        Some(now_utc())
    } else {
        None
    };
    state.publish(status);
}

fn publish_failure_once(state: &RuntimeStateHandle, error_code: &str) {
    if state.status().state != RuntimeState::Failed {
        publish_state(state, RuntimeState::Failed, Some(error_code.to_owned()));
    }
}

#[derive(Debug, thiserror::Error)]
#[error("desktop runtime failed: {error_code}")]
pub struct DesktopRuntimeError {
    error_code: String,
}

impl DesktopRuntimeError {
    fn new(error_code: impl Into<String>) -> Self {
        Self {
            error_code: error_code.into(),
        }
    }

    fn client(error: RuntimeClientError) -> Self {
        Self::new(error.error_code())
    }

    fn websocket(error: RuntimeWebSocketError) -> Self {
        Self::new(error.error_code())
    }

    fn process(error: BackendProcessError) -> Self {
        let error_code = match error {
            BackendProcessError::Io(_) => "RUNTIME_LOOPBACK_PORT_FAILED",
            BackendProcessError::Shell(_) => "SIDECAR_SPAWN_FAILED",
            BackendProcessError::Job(_) => "SIDECAR_JOB_FAILED",
            BackendProcessError::EventChannelClosed => "SIDECAR_EVENT_CHANNEL_CLOSED",
            BackendProcessError::UnexpectedStdout => "SIDECAR_STDOUT_FORBIDDEN",
            BackendProcessError::InvalidEnvironment => "SIDECAR_ENVIRONMENT_INVALID",
        };
        Self::new(error_code)
    }

    fn model(_error: RuntimeModelError) -> Self {
        Self::new("RUNTIME_INITIALIZE_CONFIGURATION_INVALID")
    }

    fn is_shutdown_requested(&self) -> bool {
        self.error_code == "RUNTIME_STARTUP_SHUTDOWN_REQUESTED"
    }

    pub fn error_code(&self) -> &str {
        &self.error_code
    }
}
