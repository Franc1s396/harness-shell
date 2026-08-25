#![cfg(target_os = "windows")]

use std::{
    env,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant},
};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use harness_shell_lib::{
    protocol::{encode_frame, FrameDecoder, FrameEnvelope, MessageType},
    sidecar::{
        job::WindowsJob,
        process::{
            heartbeat_frame, initialize_frame, shutdown_frame, validate_initialize_response,
            validate_pong, validate_ready_frame,
        },
        RuntimeState, RuntimeStatus, Supervisor, SupervisorAction, SupervisorEvent,
    },
};
use serde_json::json;
use tempfile::{tempdir, TempDir};
use time::OffsetDateTime;
use uuid::Uuid;

const FRAME_TIMEOUT: Duration = Duration::from_secs(5);

struct ProcessHarness {
    child: Child,
    stdin: Option<ChildStdin>,
    frames: Receiver<Result<FrameEnvelope, String>>,
    _extraction_directory: TempDir,
    job: WindowsJob,
}

impl ProcessHarness {
    fn spawn(executable: &Path) -> Self {
        let extraction_directory = tempdir().expect("create Sidecar extraction directory");
        let system_root = env::var_os("SystemRoot").expect("read SystemRoot");
        let job = WindowsJob::create().expect("create Sidecar Job Object");
        let mut child = Command::new(executable)
            .env_clear()
            .env("SystemRoot", &system_root)
            .env("WINDIR", &system_root)
            .env("TEMP", extraction_directory.path())
            .env("TMP", extraction_directory.path())
            .env("HARNESS_SIDECAR_JOB", job.name())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn packaged sidecar");
        let stdin = child.stdin.take().expect("capture sidecar stdin");
        let mut stdout = child.stdout.take().expect("capture sidecar stdout");
        let mut stderr = child.stderr.take().expect("capture sidecar stderr");
        let (sender, frames) = mpsc::sync_channel(16);

        thread::spawn(move || {
            let mut decoder = FrameDecoder::new();
            let mut chunk = [0_u8; 65_536];
            loop {
                match stdout.read(&mut chunk) {
                    Ok(0) => return,
                    Ok(read) => match decoder.push(&chunk[..read]) {
                        Ok(decoded) => {
                            for frame in decoded {
                                if sender.send(Ok(frame)).is_err() {
                                    return;
                                }
                            }
                        }
                        Err(error) => {
                            let _ = sender.send(Err(error.to_string()));
                            return;
                        }
                    },
                    Err(error) => {
                        let _ = sender.send(Err(error.to_string()));
                        return;
                    }
                }
            }
        });
        thread::spawn(move || {
            let mut sink = Vec::new();
            let _ = stderr.read_to_end(&mut sink);
        });

        Self {
            child,
            stdin: Some(stdin),
            frames,
            _extraction_directory: extraction_directory,
            job,
        }
    }

    fn send(&mut self, frame: &FrameEnvelope) {
        self.stdin
            .as_mut()
            .expect("sidecar stdin is open")
            .write_all(&encode_frame(frame).expect("encode outbound frame"))
            .expect("write sidecar frame");
        self.stdin
            .as_mut()
            .expect("sidecar stdin is open")
            .flush()
            .expect("flush sidecar stdin");
    }

    fn receive(&self) -> FrameEnvelope {
        self.frames
            .recv_timeout(FRAME_TIMEOUT)
            .expect("receive sidecar frame before timeout")
            .expect("decode sidecar frame")
    }

    fn wait_for_exit(&mut self) -> i32 {
        let deadline = Instant::now() + FRAME_TIMEOUT;
        loop {
            if let Some(status) = self.child.try_wait().expect("poll sidecar exit") {
                return status.code().unwrap_or(-1);
            }
            assert!(
                Instant::now() < deadline,
                "sidecar did not exit before timeout"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn kill(&mut self) {
        self.stdin.take();
        self.job.terminate().expect("terminate Sidecar Job Object");
    }

    fn assert_job_empty(&self) {
        assert_eq!(
            self.job
                .active_processes()
                .expect("query Sidecar Job Object"),
            0,
            "Sidecar Job Object retained a runtime process"
        );
    }
}

impl Drop for ProcessHarness {
    fn drop(&mut self) {
        self.stdin.take();
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

fn executable_path() -> PathBuf {
    env::var_os("HARNESS_SIDECAR_EXE")
        .map(PathBuf::from)
        .filter(|path| path.is_file())
        .expect("HARNESS_SIDECAR_EXE must point to the packaged sidecar")
}

fn initialize(harness: &mut ProcessHarness, runtime_db: &Path) {
    let ready = harness.receive();
    validate_ready_frame(&ready).expect("validate sidecar.ready");

    let initialize = initialize_frame(
        1,
        runtime_db,
        &STANDARD.encode([0x31_u8; 32]),
        &STANDARD.encode([0x57_u8; 32]),
    )
    .expect("build initialize frame");
    let request_id = initialize.request_id;
    harness.send(&initialize);
    validate_initialize_response(&harness.receive(), request_id)
        .expect("validate initialize response");
}

#[test]
fn packaged_sidecar_initializes_heartbeats_and_shuts_down_cleanly() {
    let directory = tempdir().expect("create runtime temp directory");
    let mut harness = ProcessHarness::spawn(&executable_path());
    initialize(&mut harness, &directory.path().join("runtime.sqlite3"));

    for sequence in 2..=3 {
        let ping = heartbeat_frame(sequence);
        let request_id = ping.request_id;
        harness.send(&ping);
        validate_pong(&harness.receive(), request_id).expect("validate heartbeat pong");
    }

    let final_ping = heartbeat_frame(4);
    let final_ping_request_id = final_ping.request_id;
    harness.send(&final_ping);
    let shutdown = shutdown_frame(5);
    let shutdown_request_id = shutdown.request_id;
    harness.send(&shutdown);
    validate_pong(&harness.receive(), final_ping_request_id)
        .expect("validate heartbeat pong during shutdown");
    let response = harness.receive();
    assert_eq!(response.request_id, shutdown_request_id);
    assert_eq!(response.message_type, MessageType::Response);
    assert_eq!(
        response.payload,
        json!({"result": "stopping"}).as_object().unwrap().clone()
    );
    assert_eq!(harness.wait_for_exit(), 0);
}

#[test]
fn killed_sidecar_pauses_once_without_respawn() {
    let directory = tempdir().expect("create runtime temp directory");
    let mut harness = ProcessHarness::spawn(&executable_path());
    initialize(&mut harness, &directory.path().join("runtime.sqlite3"));

    harness.kill();
    let exit_code = harness.wait_for_exit();
    harness.assert_job_empty();
    let mut supervisor = Supervisor::new(RuntimeStatus {
        state: RuntimeState::Ready,
        error_code: None,
        node: "desktop".into(),
        recoverable: false,
        correlation_id: Uuid::new_v4(),
        last_sequence: 2,
        last_heartbeat_at: Some(OffsetDateTime::now_utc()),
    });
    let transition = supervisor.transition(SupervisorEvent::ProcessExited {
        code: Some(exit_code),
    });

    assert_eq!(transition.status.state, RuntimeState::Paused);
    assert_eq!(
        transition.status.error_code.as_deref(),
        Some("SIDECAR_EXITED")
    );
    assert_eq!(
        transition
            .actions
            .iter()
            .filter(|action| **action == SupervisorAction::PublishStatus)
            .count(),
        1
    );
    assert!(!transition.actions.contains(&SupervisorAction::Spawn));
}
