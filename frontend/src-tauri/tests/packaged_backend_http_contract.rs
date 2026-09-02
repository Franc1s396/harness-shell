#![cfg(target_os = "windows")]

use std::{
    env,
    io::Read,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use harness_shell_lib::{
    commands::{ApiType, AuthKind, ConnectionProfileInput, ModelApiConfigInput},
    runtime::{
        job::WindowsJob,
        models::{
            RuntimeInitializeBody, RuntimePhase, HEARTBEAT_INTERVAL_MS, HEARTBEAT_TIMEOUT_MS,
        },
        process::{reserve_loopback_port, sidecar_args},
        websocket::{RuntimeProjection, RuntimeWebSocketConnection, WEBSOCKET_QUEUE_CAPACITY},
        CreateAgentApiConfigRequest, CreateConnectionRequest, HealthLiveRequest,
        HealthReadyRequest, InitializeRuntimeRequest, ListConnectionsRequest, OpenPtyRequest,
        RunAgentTurnRequest, RuntimeClientError, ShutdownRuntimeRequest, TypedHttpClient,
    },
    sftp::protocol::ManualSftpRuntimeClient,
    vault::CredentialId,
};
use tempfile::{tempdir, TempDir};
use tokio::sync::mpsc;
use uuid::Uuid;

const CONTRACT_TIMEOUT: Duration = Duration::from_secs(5);

struct PackagedBackendHarness {
    child: Child,
    stdout: Arc<Mutex<Vec<u8>>>,
    stderr: Arc<Mutex<Vec<u8>>>,
    readers: Vec<thread::JoinHandle<()>>,
    _extraction_directory: TempDir,
    job: WindowsJob,
}

impl PackagedBackendHarness {
    fn spawn(executable: &Path, port: u16) -> Self {
        let extraction_directory = tempdir().expect("create backend extraction directory");
        let system_root = env::var_os("SystemRoot").expect("read SystemRoot");
        let system32 = PathBuf::from(&system_root).join("System32");
        assert!(system32.is_dir(), "System32 must exist");
        let job = WindowsJob::create().expect("create backend Job Object");
        let mut child = Command::new(executable)
            .args(sidecar_args(port))
            .env_clear()
            .env("SystemRoot", &system_root)
            .env("WINDIR", &system_root)
            .env("TEMP", extraction_directory.path())
            .env("TMP", extraction_directory.path())
            .env("PATH", &system32)
            .env("USERNAME", "harness-shell")
            .env("USERPROFILE", extraction_directory.path())
            .env("HARNESS_SIDECAR_JOB", job.name())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn packaged backend");
        let stdout = Arc::new(Mutex::new(Vec::new()));
        let stderr = Arc::new(Mutex::new(Vec::new()));
        let stdout_reader = drain(
            child.stdout.take().expect("capture backend stdout"),
            stdout.clone(),
        );
        let stderr_reader = drain(
            child.stderr.take().expect("capture backend stderr"),
            stderr.clone(),
        );
        Self {
            child,
            stdout,
            stderr,
            readers: vec![stdout_reader, stderr_reader],
            _extraction_directory: extraction_directory,
            job,
        }
    }

    fn wait_for_exit(&mut self) -> i32 {
        let deadline = Instant::now() + CONTRACT_TIMEOUT;
        loop {
            if let Some(status) = self.child.try_wait().expect("poll backend exit") {
                return status.code().unwrap_or(-1);
            }
            assert!(
                Instant::now() < deadline,
                "packaged backend did not exit before timeout"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn finish_readers(&mut self) {
        for reader in self.readers.drain(..) {
            reader.join().expect("join backend pipe reader");
        }
    }

    fn assert_clean(&self, secrets: &[&str]) {
        assert!(
            self.stdout.lock().expect("lock stdout").is_empty(),
            "packaged backend emitted forbidden stdout"
        );
        let stderr = self.stderr.lock().expect("lock stderr");
        for secret in secrets {
            assert!(
                !stderr
                    .windows(secret.len())
                    .any(|value| value == secret.as_bytes()),
                "packaged backend stderr exposed secret material"
            );
        }
        assert_eq!(
            self.job
                .active_processes()
                .expect("query backend Job Object"),
            0,
            "packaged backend Job retained a process"
        );
    }
}

impl Drop for PackagedBackendHarness {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.job.terminate();
            let _ = self.child.wait();
        }
        self.finish_readers();
    }
}

fn drain<R: Read + Send + 'static>(
    mut reader: R,
    target: Arc<Mutex<Vec<u8>>>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        reader.read_to_end(&mut bytes).expect("drain backend pipe");
        *target.lock().expect("lock backend pipe capture") = bytes;
    })
}

fn executable_path() -> PathBuf {
    env::var_os("HARNESS_SIDECAR_EXE")
        .map(PathBuf::from)
        .filter(|path| path.is_file())
        .expect("HARNESS_SIDECAR_EXE must point to the packaged backend")
}

async fn wait_live(http: &TypedHttpClient) {
    let deadline = Instant::now() + CONTRACT_TIMEOUT;
    loop {
        match http.execute(HealthLiveRequest).await {
            Ok(response) => {
                assert!(response.live);
                return;
            }
            Err(RuntimeClientError::HttpTransport) if Instant::now() < deadline => {
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
            Err(error) => panic!("packaged backend liveness failed: {error}"),
        }
    }
}

#[tokio::test]
async fn packaged_backend_initializes_http_websocket_and_shuts_down_cleanly() {
    let runtime_directory = tempdir().expect("create packaged runtime directory");
    let port = reserve_loopback_port().expect("reserve dynamic loopback port");
    let mut harness = PackagedBackendHarness::spawn(&executable_path(), port);
    let http = TypedHttpClient::new(port).expect("create typed HTTP client");
    wait_live(&http).await;

    let runtime_data_key = STANDARD.encode([0x31_u8; 32]);
    let audit_hmac_key = STANDARD.encode([0x57_u8; 32]);
    let initialized = http
        .execute(InitializeRuntimeRequest(
            RuntimeInitializeBody::new(
                "packaged-contract",
                runtime_directory
                    .path()
                    .join("runtime.sqlite3")
                    .to_string_lossy(),
                runtime_data_key.clone(),
                audit_hmac_key.clone(),
                HEARTBEAT_INTERVAL_MS,
                HEARTBEAT_TIMEOUT_MS,
            )
            .expect("build runtime initialize body"),
        ))
        .await
        .expect("initialize packaged backend");
    assert_eq!(initialized.state, RuntimePhase::Ready);
    let ready = http
        .execute(HealthReadyRequest)
        .await
        .expect("query packaged readiness");
    assert!(ready.ready);
    assert_eq!(ready.state, RuntimePhase::Ready);

    let (projection_tx, _projection_rx) =
        mpsc::channel::<RuntimeProjection>(WEBSOCKET_QUEUE_CAPACITY);
    let websocket = RuntimeWebSocketConnection::connect(port, projection_tx)
        .await
        .expect("complete packaged WebSocket heartbeat handshake");
    let (websocket_handle, mut websocket_task) = websocket.split();

    let credential_id = CredentialId::new();
    let created = http
        .execute(CreateConnectionRequest(ConnectionProfileInput {
            display_name: "packaged-http".into(),
            group_name: None,
            host: "127.0.0.1".into(),
            port: 1,
            username: "deploy".into(),
            auth_kind: AuthKind::Password,
            credential_id,
            passphrase_credential_id: None,
            proxy_jump_id: None,
            favorite: false,
        }))
        .await
        .expect("create packaged connection")
        .connection;
    let listed = http
        .execute(ListConnectionsRequest)
        .await
        .expect("list packaged connections");
    assert_eq!(listed.connections.len(), 1);
    assert_eq!(listed.connections[0].connection_id, created.connection_id);

    let missing_session = Uuid::new_v4();
    let pty_error = http
        .execute(OpenPtyRequest {
            ssh_session_id: missing_session,
            cols: 80,
            rows: 24,
        })
        .await
        .expect_err("missing SSH session must reject PTY open");
    assert_eq!(pty_error.error_code(), "SSH_SESSION_NOT_FOUND");

    let sftp = ManualSftpRuntimeClient::new(
        harness_shell_lib::runtime::RuntimeClientHandle::new_http_only(http.clone()),
    );
    let sftp_error = sftp
        .open(missing_session)
        .await
        .expect_err("missing SSH session must reject SFTP open");
    assert_eq!(sftp_error.code(), "SFTP_SESSION_NOT_CONNECTED");

    let api_key_credential_id = CredentialId::new();
    let config = http
        .execute(CreateAgentApiConfigRequest(ModelApiConfigInput {
            display_name: "packaged-agent".into(),
            api_type: ApiType::Responses,
            base_url: "https://example.invalid/v1".into(),
            model: "test-model".into(),
            api_key_secret_ref: api_key_credential_id,
            enabled: true,
        }))
        .await
        .expect("create packaged Agent config")
        .config;
    let api_key = STANDARD.encode(b"packaged-agent-secret");
    let agent_error = http
        .execute(RunAgentTurnRequest::new(
            None,
            missing_session,
            config.api_config_id,
            api_key_credential_id,
            api_key.clone(),
            "pwd".into(),
        ))
        .await
        .expect_err("missing SSH session must reject Agent turn");
    assert_eq!(agent_error.error_code(), "SSH_SESSION_UNAVAILABLE");

    websocket_handle
        .shutdown()
        .await
        .expect("close packaged WebSocket");
    tokio::time::timeout(Duration::from_secs(1), &mut websocket_task)
        .await
        .expect("WebSocket actor must stop")
        .expect("WebSocket task must join")
        .expect("WebSocket actor must close cleanly");
    let stopped = http
        .execute(ShutdownRuntimeRequest)
        .await
        .expect("shutdown packaged backend");
    assert_eq!(stopped.state, RuntimePhase::Stopped);
    assert_eq!(harness.wait_for_exit(), 0);
    harness.finish_readers();
    harness.assert_clean(&[&runtime_data_key, &audit_hmac_key, &api_key]);
}
