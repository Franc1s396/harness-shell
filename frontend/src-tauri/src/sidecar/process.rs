use std::{
    collections::{HashSet, VecDeque},
    env,
    future::Future,
    path::{Path, PathBuf},
    time::Duration,
};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde_json::{json, Map, Value};
use tauri::{async_runtime::Receiver, AppHandle, Emitter, Runtime};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
use time::OffsetDateTime;
use tokio::sync::mpsc::{Receiver as BrokerReceiver, UnboundedReceiver as ControlReceiver};
use tokio::time::Instant;
use uuid::Uuid;
use zeroize::Zeroizing;

use crate::protocol::{
    encode_frame, FrameDecoder, FrameEnvelope, MessageType, ProtocolError, Sensitivity,
    PROTOCOL_VERSION,
};
use crate::{
    app_state::{RuntimeControl, RuntimeStateHandle},
    sidecar::{
        broker::{
            emit_runtime_event_projection, BrokerError, PendingReplies, RuntimeCommand,
            RuntimeRequest,
        },
        job::WindowsJob,
        Supervisor, SupervisorEvent,
    },
    vault::RuntimeKeys,
};

const STDERR_LINE_LIMIT: usize = 16 * 1_024;
const READY_TIMEOUT: Duration = Duration::from_secs(5);
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(15);
const SHUTDOWN_GRACE: Duration = Duration::from_secs(3);

#[derive(Debug, thiserror::Error)]
pub enum ProcessError {
    #[error("packaged Sidecar operation failed")]
    Shell(#[from] tauri_plugin_shell::Error),
    #[error("packaged Sidecar process tree ownership failed")]
    Job(#[from] super::job::JobError),
    #[error("packaged Sidecar protocol failed: {0}")]
    Protocol(#[from] ProtocolError),
    #[error("packaged Sidecar event channel closed")]
    EventChannelClosed,
    #[error("packaged Sidecar operation timed out")]
    Timeout,
    #[error("packaged Sidecar exited unexpectedly")]
    Exited { code: Option<i32> },
    #[error("runtime database path must be absolute UTF-8")]
    InvalidRuntimePath,
    #[error("required Windows Sidecar environment is unavailable")]
    InvalidEnvironment,
    #[error("runtime key material must be canonical base64 for exactly 32 bytes")]
    InvalidRuntimeKey,
    #[error("packaged Sidecar emitted an invalid {0} frame")]
    InvalidFrame(&'static str),
    #[error("packaged Sidecar rejected initialization")]
    InitializeRejected { error_code: String },
    #[error("runtime state machine stopped the Sidecar")]
    SupervisorStopped,
    #[error("application shutdown was requested during Sidecar startup")]
    StartupShutdownRequested,
}

enum RuntimeWake {
    Control(Option<RuntimeControl>),
    Command(Option<RuntimeCommand>),
    Output(Result<SidecarOutput, ProcessError>),
    HeartbeatDue,
    HeartbeatTimedOut,
}

async fn wait_for_runtime_wake<F>(
    control: &mut ControlReceiver<RuntimeControl>,
    broker_commands: &mut BrokerReceiver<RuntimeCommand>,
    sidecar_output: F,
    next_heartbeat: Instant,
    heartbeat_deadline: Instant,
    control_open: bool,
) -> RuntimeWake
where
    F: Future<Output = Result<SidecarOutput, ProcessError>>,
{
    tokio::select! {
        control = control.recv(), if control_open => RuntimeWake::Control(control),
        command = broker_commands.recv() => RuntimeWake::Command(command),
        output = sidecar_output => RuntimeWake::Output(output),
        _ = tokio::time::sleep_until(next_heartbeat) => RuntimeWake::HeartbeatDue,
        _ = tokio::time::sleep_until(heartbeat_deadline) => RuntimeWake::HeartbeatTimedOut,
    }
}

pub async fn supervise_runtime<R: Runtime>(
    app: AppHandle<R>,
    state: RuntimeStateHandle,
    mut control: ControlReceiver<RuntimeControl>,
    mut broker_commands: BrokerReceiver<RuntimeCommand>,
    runtime_db_path: &Path,
    extraction_directory: &Path,
    runtime_keys: RuntimeKeys,
) -> Result<(), ProcessError> {
    let mut supervisor = Supervisor::new(state.status());
    let runtime_data_key_b64 = Zeroizing::new(STANDARD.encode(&runtime_keys.runtime_data_key));
    let audit_hmac_key_b64 = Zeroizing::new(STANDARD.encode(&runtime_keys.audit_hmac_key));
    let forbidden = vec![
        Zeroizing::new(runtime_data_key_b64.as_bytes().to_vec()),
        Zeroizing::new(audit_hmac_key_b64.as_bytes().to_vec()),
    ];
    let mut process = match SidecarProcess::spawn(&app, extraction_directory, forbidden) {
        Ok(process) => process,
        Err(error) => {
            publish(&state, supervisor.transition(SupervisorEvent::SpawnFailed));
            return Err(error);
        }
    };

    let ready = match next_startup_output(&mut process, &mut control, READY_TIMEOUT).await {
        Ok(SidecarOutput::Frame(frame)) => frame,
        Ok(SidecarOutput::Terminated { code }) => {
            publish(
                &state,
                supervisor.transition(SupervisorEvent::ProcessExited { code }),
            );
            return Err(ProcessError::Exited { code });
        }
        Err(error) => {
            if matches!(error, ProcessError::StartupShutdownRequested) {
                publish(
                    &state,
                    supervisor.transition(SupervisorEvent::ShutdownCompleted),
                );
                process.kill()?;
                return Ok(());
            }
            publish_error_if_active(
                &state,
                &mut supervisor,
                &error,
                SupervisorEvent::InvalidFrame,
            );
            process.kill()?;
            return Err(error);
        }
    };
    if let Err(error) = validate_ready_frame(&ready) {
        publish(&state, supervisor.transition(SupervisorEvent::InvalidFrame));
        process.kill()?;
        return Err(error);
    }
    let mut next_inbound_sequence = 2;
    publish(&state, supervisor.transition(SupervisorEvent::Spawned));

    let mut initialize = match initialize_frame(
        1,
        runtime_db_path,
        &runtime_data_key_b64,
        &audit_hmac_key_b64,
    ) {
        Ok(frame) => frame,
        Err(error) => {
            publish(
                &state,
                supervisor.transition(SupervisorEvent::InvalidInitializeResponse),
            );
            process.kill()?;
            return Err(error);
        }
    };
    let initialize_request_id = initialize.request_id;
    let write_result = process.write_frame(&initialize);
    zeroize_initialize_payload(&mut initialize);
    if let Err(error) = write_result {
        publish(
            &state,
            supervisor.transition(SupervisorEvent::InvalidInitializeResponse),
        );
        process.kill()?;
        return Err(error);
    }

    let initialized = match receive_expected_frame(
        &mut process,
        &mut supervisor,
        &state,
        READY_TIMEOUT,
        &mut next_inbound_sequence,
        &mut control,
    )
    .await
    {
        Ok(frame) => frame,
        Err(error) => {
            if matches!(error, ProcessError::StartupShutdownRequested) {
                publish(
                    &state,
                    supervisor.transition(SupervisorEvent::ShutdownCompleted),
                );
                process.kill()?;
                return Ok(());
            }
            publish_error_if_active(
                &state,
                &mut supervisor,
                &error,
                SupervisorEvent::InvalidInitializeResponse,
            );
            process.kill()?;
            return Err(error);
        }
    };
    if let Err(error) = validate_initialize_response(&initialized, initialize_request_id) {
        publish_error_if_active(
            &state,
            &mut supervisor,
            &error,
            SupervisorEvent::InvalidInitializeResponse,
        );
        process.kill()?;
        return Err(error);
    }
    publish(
        &state,
        supervisor.transition(SupervisorEvent::InitializeAccepted),
    );

    let mut next_outbound_sequence = 2;
    let mut next_heartbeat = Instant::now() + HEARTBEAT_INTERVAL;
    let mut last_valid_pong = Instant::now();
    let mut pending_pings: HashSet<Uuid> = HashSet::new();
    let mut pending_replies = PendingReplies::new();
    let mut control_open = true;

    loop {
        let wake = wait_for_runtime_wake(
            &mut control,
            &mut broker_commands,
            process.next_output(),
            next_heartbeat,
            last_valid_pong + HEARTBEAT_TIMEOUT,
            control_open,
        )
        .await;

        match wake {
            RuntimeWake::Control(Some(RuntimeControl::Shutdown))
            | RuntimeWake::Command(Some(RuntimeCommand::Shutdown))
            | RuntimeWake::Command(None) => {
                pending_replies.fail_all(BrokerError::Closed);
                return shutdown_runtime(
                    &app,
                    &mut process,
                    &mut supervisor,
                    &state,
                    next_outbound_sequence,
                    &mut next_inbound_sequence,
                    &mut pending_pings,
                    &mut pending_replies,
                )
                .await;
            }
            RuntimeWake::Control(None) => {
                control_open = false;
            }
            RuntimeWake::Command(Some(command)) => match command {
                RuntimeCommand::Request {
                    request_id,
                    request,
                    reply,
                } => {
                    let mut frame =
                        application_request_frame(next_outbound_sequence, request_id, request);
                    let write_result = process.write_frame(&frame);
                    zeroize_sensitive_frame(&mut frame);
                    if let Err(error) = write_result {
                        let _ = reply.send(Err(BrokerError::Protocol));
                        pending_replies.fail_all(BrokerError::Protocol);
                        publish(&state, supervisor.transition(SupervisorEvent::InvalidFrame));
                        process.kill()?;
                        return Err(error);
                    }
                    pending_replies
                        .insert(request_id, reply)
                        .map_err(|_| ProcessError::InvalidFrame("duplicate broker request"))?;
                    next_outbound_sequence += 1;
                }
                RuntimeCommand::Cancel {
                    target_request_id,
                    reply,
                } => {
                    let frame = cancellation_frame(next_outbound_sequence, target_request_id);
                    let request_id = frame.request_id;
                    if let Err(error) = process.write_frame(&frame) {
                        let _ = reply.send(Err(BrokerError::Protocol));
                        pending_replies.fail_all(BrokerError::Protocol);
                        publish(&state, supervisor.transition(SupervisorEvent::InvalidFrame));
                        process.kill()?;
                        return Err(error);
                    }
                    pending_replies
                        .insert(request_id, reply)
                        .map_err(|_| ProcessError::InvalidFrame("duplicate broker cancel"))?;
                    next_outbound_sequence += 1;
                }
                RuntimeCommand::Shutdown => {
                    unreachable!("shutdown handled before command dispatch")
                }
            },
            RuntimeWake::HeartbeatDue => {
                let heartbeat = heartbeat_frame(next_outbound_sequence);
                pending_pings.insert(heartbeat.request_id);
                if let Err(error) = process.write_frame(&heartbeat) {
                    publish(&state, supervisor.transition(SupervisorEvent::InvalidFrame));
                    process.kill()?;
                    return Err(error);
                }
                next_outbound_sequence += 1;
                next_heartbeat += HEARTBEAT_INTERVAL;
            }
            RuntimeWake::HeartbeatTimedOut => {
                let transition =
                    apply_heartbeat_timeout(&mut supervisor, last_valid_pong, Instant::now())
                        .expect("heartbeat deadline wake must produce a transition");
                publish(&state, transition);
                process.kill()?;
                return Err(ProcessError::SupervisorStopped);
            }
            RuntimeWake::Output(Ok(SidecarOutput::Frame(frame))) => {
                if frame.sequence != next_inbound_sequence {
                    publish(
                        &state,
                        supervisor.transition(SupervisorEvent::SequenceGap {
                            expected: next_inbound_sequence,
                            actual: frame.sequence,
                        }),
                    );
                    process.kill()?;
                    return Err(ProcessError::SupervisorStopped);
                }
                next_inbound_sequence += 1;
                let frame_received_at = Instant::now();
                last_valid_pong =
                    advance_heartbeat_clock(last_valid_pong, frame_received_at, false);
                if pending_pings.remove(&frame.request_id) {
                    if let Err(error) = validate_pong(&frame, frame.request_id) {
                        publish(&state, supervisor.transition(SupervisorEvent::InvalidFrame));
                        process.kill()?;
                        return Err(error);
                    }
                    last_valid_pong =
                        advance_heartbeat_clock(last_valid_pong, frame_received_at, true);
                    publish(
                        &state,
                        supervisor.transition(SupervisorEvent::HeartbeatReceived {
                            sequence: frame.sequence,
                            at: frame.timestamp,
                        }),
                    );
                } else if frame.message_type == MessageType::Event {
                    if emit_runtime_event_projection(&frame.payload, |projection| {
                        app.emit_to("main", projection.webview_event, projection.payload.clone())
                    })
                    .is_err()
                    {
                        publish(&state, supervisor.transition(SupervisorEvent::InvalidFrame));
                        process.kill()?;
                        return Err(ProcessError::InvalidFrame("runtime event"));
                    }
                } else if pending_replies.contains(frame.request_id) {
                    pending_replies
                        .complete(frame)
                        .map_err(|_| ProcessError::InvalidFrame("broker response"))?;
                } else {
                    publish(&state, supervisor.transition(SupervisorEvent::InvalidFrame));
                    process.kill()?;
                    return Err(ProcessError::InvalidFrame("unsolicited runtime frame"));
                }
            }
            RuntimeWake::Output(Ok(SidecarOutput::Terminated { code })) => {
                pending_replies.fail_all(BrokerError::Closed);
                publish(
                    &state,
                    supervisor.transition(SupervisorEvent::ProcessExited { code }),
                );
                return Err(ProcessError::Exited { code });
            }
            RuntimeWake::Output(Err(error)) => {
                pending_replies.fail_all(BrokerError::Protocol);
                publish_error_if_active(
                    &state,
                    &mut supervisor,
                    &error,
                    SupervisorEvent::InvalidFrame,
                );
                process.kill()?;
                return Err(error);
            }
        }
    }
}

async fn receive_expected_frame(
    process: &mut SidecarProcess,
    supervisor: &mut Supervisor,
    state: &RuntimeStateHandle,
    timeout: Duration,
    next_inbound_sequence: &mut u64,
    control: &mut ControlReceiver<RuntimeControl>,
) -> Result<FrameEnvelope, ProcessError> {
    match next_startup_output(process, control, timeout).await? {
        SidecarOutput::Frame(frame) => {
            if frame.sequence != *next_inbound_sequence {
                publish(
                    state,
                    supervisor.transition(SupervisorEvent::SequenceGap {
                        expected: *next_inbound_sequence,
                        actual: frame.sequence,
                    }),
                );
                process.kill()?;
                return Err(ProcessError::SupervisorStopped);
            }
            *next_inbound_sequence += 1;
            Ok(frame)
        }
        SidecarOutput::Terminated { code } => {
            publish(
                state,
                supervisor.transition(SupervisorEvent::ProcessExited { code }),
            );
            Err(ProcessError::Exited { code })
        }
    }
}

async fn next_startup_output(
    process: &mut SidecarProcess,
    control: &mut ControlReceiver<RuntimeControl>,
    timeout: Duration,
) -> Result<SidecarOutput, ProcessError> {
    let deadline = Instant::now() + timeout;
    let mut control_open = true;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(ProcessError::Timeout);
        }
        tokio::select! {
            biased;
            control = control.recv(), if control_open => match control {
                Some(RuntimeControl::Shutdown) => {
                    return Err(ProcessError::StartupShutdownRequested);
                }
                None => {
                    control_open = false;
                }
            },
            output = tokio::time::timeout(remaining, process.next_output()) => {
                return output.map_err(|_| ProcessError::Timeout)?;
            }
        }
    }
}

async fn shutdown_runtime<R: Runtime>(
    app: &AppHandle<R>,
    process: &mut SidecarProcess,
    supervisor: &mut Supervisor,
    state: &RuntimeStateHandle,
    sequence: u64,
    next_inbound_sequence: &mut u64,
    pending_pings: &mut HashSet<Uuid>,
    pending_replies: &mut PendingReplies,
) -> Result<(), ProcessError> {
    let transition = supervisor.transition(SupervisorEvent::ShutdownRequested);
    publish(state, transition);
    let shutdown = shutdown_frame(sequence);
    let request_id = shutdown.request_id;
    if let Err(error) = process.write_frame(&shutdown) {
        publish(state, supervisor.transition(SupervisorEvent::InvalidFrame));
        if let Err(kill_error) = process.kill() {
            log::error!(target: "harness_shell::sidecar", "secondary cleanup failed: {kill_error}");
        }
        return Err(error);
    }
    let deadline = Instant::now() + SHUTDOWN_GRACE;
    let mut acknowledged = false;

    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match tokio::time::timeout(remaining, process.next_output()).await {
            Err(_) => break,
            Ok(output) => match output {
                Ok(SidecarOutput::Frame(frame)) => {
                    if frame.sequence != *next_inbound_sequence {
                        publish(state, supervisor.transition(SupervisorEvent::InvalidFrame));
                        if let Err(kill_error) = process.kill() {
                            log::error!(target: "harness_shell::sidecar", "secondary cleanup failed: {kill_error}");
                        }
                        return Err(ProcessError::InvalidFrame("shutdown response"));
                    }
                    *next_inbound_sequence += 1;
                    if frame.request_id == request_id
                        && frame.message_type == MessageType::Response
                        && frame.payload == object(json!({"result": "stopping"}))
                    {
                        acknowledged = true;
                        continue;
                    }
                    if pending_pings.remove(&frame.request_id) {
                        if let Err(error) = validate_pong(&frame, frame.request_id) {
                            publish(state, supervisor.transition(SupervisorEvent::InvalidFrame));
                            if let Err(kill_error) = process.kill() {
                                log::error!(target: "harness_shell::sidecar", "secondary cleanup failed: {kill_error}");
                            }
                            return Err(error);
                        }
                    } else if frame.message_type == MessageType::Event {
                        if emit_runtime_event_projection(&frame.payload, |projection| {
                            app.emit_to(
                                "main",
                                projection.webview_event,
                                projection.payload.clone(),
                            )
                        })
                        .is_err()
                        {
                            publish(state, supervisor.transition(SupervisorEvent::InvalidFrame));
                            process.kill()?;
                            return Err(ProcessError::InvalidFrame("shutdown event"));
                        }
                    } else if pending_replies.contains(frame.request_id) {
                        pending_replies
                            .complete(frame)
                            .map_err(|_| ProcessError::InvalidFrame("shutdown broker response"))?;
                    } else {
                        publish(state, supervisor.transition(SupervisorEvent::InvalidFrame));
                        process.kill()?;
                        return Err(ProcessError::InvalidFrame("shutdown response"));
                    }
                }
                Ok(SidecarOutput::Terminated { code }) => {
                    if !acknowledged || code != Some(0) {
                        publish(state, supervisor.transition(SupervisorEvent::InvalidFrame));
                        return Err(ProcessError::Exited { code });
                    }
                    publish(
                        state,
                        supervisor.transition(SupervisorEvent::ProcessExited { code }),
                    );
                    pending_replies.fail_all(BrokerError::Closed);
                    return Ok(());
                }
                Err(ProcessError::Timeout) => break,
                Err(error) => {
                    publish(state, supervisor.transition(SupervisorEvent::InvalidFrame));
                    if let Err(kill_error) = process.kill() {
                        log::error!(target: "harness_shell::sidecar", "secondary cleanup failed: {kill_error}");
                    }
                    return Err(error);
                }
            },
        }
    }

    publish(
        state,
        supervisor.transition(SupervisorEvent::ShutdownTimedOut),
    );
    pending_replies.fail_all(BrokerError::Closed);
    process.kill()?;
    Err(ProcessError::Timeout)
}

fn zeroize_initialize_payload(frame: &mut FrameEnvelope) {
    for field in ["runtime_data_key_b64", "audit_hmac_key_b64"] {
        if let Some(Value::String(value)) = frame.payload.get_mut(field) {
            use zeroize::Zeroize;
            value.zeroize();
        }
    }
    frame.payload.clear();
}

fn zeroize_sensitive_frame(frame: &mut FrameEnvelope) {
    if frame.sensitivity != Sensitivity::Secret {
        return;
    }
    for value in frame.payload.values_mut() {
        zeroize_json_value(value);
    }
    frame.payload.clear();
}

fn zeroize_json_value(value: &mut Value) {
    match value {
        Value::String(text) => {
            use zeroize::Zeroize;
            text.zeroize();
        }
        Value::Array(values) => {
            for value in values.iter_mut() {
                zeroize_json_value(value);
            }
            values.clear();
        }
        Value::Object(values) => {
            for value in values.values_mut() {
                zeroize_json_value(value);
            }
            values.clear();
        }
        Value::Null | Value::Bool(_) | Value::Number(_) => {}
    }
}

fn publish(state: &RuntimeStateHandle, transition: super::SupervisorTransition) {
    state.publish(transition.status);
}

#[derive(Debug)]
pub enum SidecarOutput {
    Frame(FrameEnvelope),
    Terminated { code: Option<i32> },
}

pub struct SidecarProcess {
    events: Receiver<CommandEvent>,
    child: Option<CommandChild>,
    decoder: FrameDecoder,
    pending_frames: VecDeque<FrameEnvelope>,
    stderr_buffer: Vec<u8>,
    discarding_stderr_line: bool,
    forbidden_stderr_fragments: Vec<Zeroizing<Vec<u8>>>,
    job: WindowsJob,
}

impl SidecarProcess {
    pub fn spawn<R: Runtime>(
        app: &AppHandle<R>,
        extraction_directory: &Path,
        forbidden_stderr_fragments: Vec<Zeroizing<Vec<u8>>>,
    ) -> Result<Self, ProcessError> {
        let extraction_directory = extraction_directory
            .to_str()
            .filter(|_| extraction_directory.is_absolute())
            .ok_or(ProcessError::InvalidEnvironment)?;
        let system_root = env::var_os("SystemRoot").ok_or(ProcessError::InvalidEnvironment)?;
        let system32 = PathBuf::from(&system_root).join("System32");
        if !system32.is_dir() {
            return Err(ProcessError::InvalidEnvironment);
        }
        let job = WindowsJob::create()?;
        let (events, child) = app
            .shell()
            .sidecar("harness-shell-sidecar")?
            .set_raw_out(true)
            .env_clear()
            .env("SystemRoot", &system_root)
            .env("WINDIR", &system_root)
            .env("TEMP", extraction_directory)
            .env("TMP", extraction_directory)
            .env("PATH", &system32)
            .env("USERNAME", "harness-shell")
            .env("USERPROFILE", extraction_directory)
            .env("HARNESS_SIDECAR_JOB", job.name())
            .spawn()?;
        Ok(Self {
            events,
            child: Some(child),
            decoder: FrameDecoder::new(),
            pending_frames: VecDeque::new(),
            stderr_buffer: Vec::new(),
            discarding_stderr_line: false,
            forbidden_stderr_fragments,
            job,
        })
    }

    pub fn pid(&self) -> Option<u32> {
        self.child.as_ref().map(CommandChild::pid)
    }

    pub fn write_frame(&mut self, frame: &FrameEnvelope) -> Result<(), ProcessError> {
        let encoded = Zeroizing::new(encode_frame(frame)?);
        let child = self
            .child
            .as_mut()
            .ok_or(ProcessError::EventChannelClosed)?;
        child.write(encoded.as_slice())?;
        Ok(())
    }

    pub async fn next_output(&mut self) -> Result<SidecarOutput, ProcessError> {
        if let Some(frame) = self.pending_frames.pop_front() {
            return Ok(SidecarOutput::Frame(frame));
        }

        loop {
            let event = self
                .events
                .recv()
                .await
                .ok_or(ProcessError::EventChannelClosed)?;
            match event {
                CommandEvent::Stdout(bytes) => {
                    self.pending_frames.extend(self.decoder.push(&bytes)?);
                    if let Some(frame) = self.pending_frames.pop_front() {
                        return Ok(SidecarOutput::Frame(frame));
                    }
                }
                CommandEvent::Stderr(bytes) => self.consume_stderr(&bytes),
                CommandEvent::Terminated(payload) => {
                    self.flush_stderr();
                    self.child = None;
                    return Ok(SidecarOutput::Terminated { code: payload.code });
                }
                CommandEvent::Error(_) => return Err(ProcessError::EventChannelClosed),
                _ => return Err(ProcessError::EventChannelClosed),
            }
        }
    }

    pub fn kill(&mut self) -> Result<(), ProcessError> {
        self.job.terminate()?;
        self.child.take();
        Ok(())
    }

    fn consume_stderr(&mut self, bytes: &[u8]) {
        self.stderr_buffer.extend_from_slice(bytes);
        loop {
            if self.discarding_stderr_line {
                let Some(delimiter) = self
                    .stderr_buffer
                    .iter()
                    .position(|byte| matches!(*byte, b'\r' | b'\n'))
                else {
                    self.stderr_buffer.clear();
                    return;
                };
                self.stderr_buffer.drain(..=delimiter);
                self.discarding_stderr_line = false;
                continue;
            }

            if let Some(delimiter) = self
                .stderr_buffer
                .iter()
                .position(|byte| matches!(*byte, b'\r' | b'\n'))
            {
                let mut line: Vec<u8> = self.stderr_buffer.drain(..=delimiter).collect();
                while line
                    .last()
                    .is_some_and(|byte| matches!(*byte, b'\r' | b'\n'))
                {
                    line.pop();
                }
                self.log_stderr_line(&line, false);
                continue;
            }

            if self.stderr_buffer.len() > STDERR_LINE_LIMIT {
                self.stderr_buffer.clear();
                self.discarding_stderr_line = true;
                log::warn!(
                    target: "harness_shell::sidecar",
                    "sidecar stderr line exceeded {STDERR_LINE_LIMIT} bytes and was redacted"
                );
            }
            return;
        }
    }

    fn log_stderr_line(&self, line: &[u8], truncated: bool) {
        if line.is_empty() {
            return;
        }
        if self.forbidden_stderr_fragments.iter().any(|fragment| {
            !fragment.is_empty()
                && line
                    .windows(fragment.len())
                    .any(|window| window == fragment.as_slice())
        }) {
            log::warn!(target: "harness_shell::sidecar", "sidecar stderr line redacted");
            return;
        }

        let line = String::from_utf8_lossy(line);
        if truncated {
            log::warn!(target: "harness_shell::sidecar", "{line} [truncated]");
        } else {
            log::warn!(target: "harness_shell::sidecar", "{line}");
        }
    }

    fn flush_stderr(&mut self) {
        if !self.discarding_stderr_line && !self.stderr_buffer.is_empty() {
            let line = std::mem::take(&mut self.stderr_buffer);
            self.log_stderr_line(&line, false);
        } else {
            self.stderr_buffer.clear();
        }
        self.discarding_stderr_line = false;
    }
}

pub fn initialize_frame(
    sequence: u64,
    runtime_db_path: &Path,
    runtime_data_key_b64: &str,
    audit_hmac_key_b64: &str,
) -> Result<FrameEnvelope, ProcessError> {
    let runtime_db_path = runtime_db_path
        .to_str()
        .filter(|_| runtime_db_path.is_absolute())
        .ok_or(ProcessError::InvalidRuntimePath)?;
    validate_runtime_key(runtime_data_key_b64)?;
    validate_runtime_key(audit_hmac_key_b64)?;
    Ok(frame(
        MessageType::Request,
        sequence,
        Sensitivity::Secret,
        json!({
            "method": "initialize",
            "app_version": env!("CARGO_PKG_VERSION"),
            "runtime_db_path": runtime_db_path,
            "runtime_data_key_b64": runtime_data_key_b64,
            "audit_hmac_key_b64": audit_hmac_key_b64,
            "heartbeat_interval_ms": 5_000,
            "heartbeat_timeout_ms": 15_000
        }),
    ))
}

pub fn heartbeat_frame(sequence: u64) -> FrameEnvelope {
    frame(
        MessageType::Heartbeat,
        sequence,
        Sensitivity::Normal,
        json!({"kind": "ping"}),
    )
}

pub fn application_request_frame(
    sequence: u64,
    request_id: Uuid,
    request: RuntimeRequest,
) -> FrameEnvelope {
    FrameEnvelope {
        protocol_version: PROTOCOL_VERSION,
        message_type: MessageType::Request,
        request_id,
        task_id: request.task_id,
        workflow_run_id: request.workflow_run_id,
        sequence,
        timestamp: OffsetDateTime::now_utc(),
        sensitivity: request.sensitivity,
        payload: request.payload,
    }
}

pub fn cancellation_frame(sequence: u64, target_request_id: Uuid) -> FrameEnvelope {
    frame(
        MessageType::Cancel,
        sequence,
        Sensitivity::Normal,
        json!({
            "target_request_id": target_request_id,
            "reason": "user_requested"
        }),
    )
}

pub fn shutdown_frame(sequence: u64) -> FrameEnvelope {
    frame(
        MessageType::Request,
        sequence,
        Sensitivity::Normal,
        json!({"method": "shutdown"}),
    )
}

pub fn validate_ready_frame(frame: &FrameEnvelope) -> Result<(), ProcessError> {
    let capabilities = frame.payload.get("capabilities").and_then(Value::as_object);
    let supports_v1 = capabilities
        .and_then(|value| value.get("protocol_versions"))
        .and_then(Value::as_array)
        .is_some_and(|versions| versions.contains(&json!(PROTOCOL_VERSION)));
    let schema_v3 =
        capabilities.and_then(|value| value.get("storage_schema_version")) == Some(&json!(3));
    let features = capabilities
        .and_then(|value| value.get("features"))
        .and_then(Value::as_array);
    let required_features = [
        "connection_profiles",
        "host_key_store",
        "ssh_runtime",
        "pty",
        "manual_sftp",
    ];
    let supports_required_features = features.is_some_and(|features| {
        required_features
            .iter()
            .all(|feature| features.contains(&json!(feature)))
    });
    if frame.message_type != MessageType::Event
        || frame.sequence != 1
        || frame.sensitivity != Sensitivity::Normal
        || frame.payload.get("event") != Some(&json!("sidecar.ready"))
        || !supports_v1
        || !schema_v3
        || !supports_required_features
    {
        return Err(ProcessError::InvalidFrame("sidecar.ready"));
    }
    Ok(())
}

pub fn validate_initialize_response(
    frame: &FrameEnvelope,
    request_id: Uuid,
) -> Result<(), ProcessError> {
    if frame.request_id != request_id {
        return Err(ProcessError::InvalidFrame("initialize response"));
    }

    if frame.message_type == MessageType::Error {
        let error_code = frame
            .payload
            .get("error_code")
            .and_then(Value::as_str)
            .and_then(allowed_initialize_error_code)
            .ok_or(ProcessError::InvalidFrame("initialize error"))?;
        return Err(ProcessError::InitializeRejected {
            error_code: error_code.into(),
        });
    }

    if frame.message_type != MessageType::Response
        || frame.payload != object(json!({"result": "initialized", "state": "READY"}))
    {
        return Err(ProcessError::InvalidFrame("initialize response"));
    }
    Ok(())
}

pub fn heartbeat_timed_out(last_valid: Instant, now: Instant) -> bool {
    now.duration_since(last_valid) >= HEARTBEAT_TIMEOUT
}

pub fn advance_heartbeat_clock(
    last_valid_pong: Instant,
    frame_received_at: Instant,
    valid_pong: bool,
) -> Instant {
    if valid_pong {
        frame_received_at
    } else {
        last_valid_pong
    }
}

pub fn apply_heartbeat_timeout(
    supervisor: &mut Supervisor,
    last_valid: Instant,
    now: Instant,
) -> Option<super::SupervisorTransition> {
    heartbeat_timed_out(last_valid, now)
        .then(|| supervisor.transition(SupervisorEvent::HeartbeatTimedOut))
}

pub fn validate_pong(frame: &FrameEnvelope, request_id: Uuid) -> Result<(), ProcessError> {
    if frame.request_id != request_id
        || frame.message_type != MessageType::Heartbeat
        || frame.payload != object(json!({"kind": "pong"}))
    {
        return Err(ProcessError::InvalidFrame("heartbeat pong"));
    }
    Ok(())
}

fn validate_runtime_key(encoded: &str) -> Result<(), ProcessError> {
    let decoded = Zeroizing::new(
        STANDARD
            .decode(encoded)
            .map_err(|_| ProcessError::InvalidRuntimeKey)?,
    );
    if decoded.len() != 32 || STANDARD.encode(decoded.as_slice()) != encoded {
        return Err(ProcessError::InvalidRuntimeKey);
    }
    Ok(())
}

fn allowed_initialize_error_code(value: &str) -> Option<&'static str> {
    match value {
        "AUDIT_CHAIN_INVALID" => Some("AUDIT_CHAIN_INVALID"),
        "RUNTIME_INITIALIZATION_FAILED" => Some("RUNTIME_INITIALIZATION_FAILED"),
        "INVALID_INITIALIZE_PAYLOAD" => Some("INVALID_INITIALIZE_PAYLOAD"),
        "SENSITIVE_FRAME_REQUIRED" => Some("SENSITIVE_FRAME_REQUIRED"),
        "INVALID_RUNTIME_PHASE" => Some("INVALID_RUNTIME_PHASE"),
        _ => None,
    }
}

fn frame(
    message_type: MessageType,
    sequence: u64,
    sensitivity: Sensitivity,
    payload: Value,
) -> FrameEnvelope {
    FrameEnvelope {
        protocol_version: PROTOCOL_VERSION,
        message_type,
        request_id: Uuid::new_v4(),
        task_id: None,
        workflow_run_id: None,
        sequence,
        timestamp: OffsetDateTime::now_utc(),
        sensitivity,
        payload: object(payload),
    }
}

fn object(value: Value) -> Map<String, Value> {
    value
        .as_object()
        .expect("protocol payload literals must be objects")
        .clone()
}

pub fn supervisor_event_for_process_error(
    error: &ProcessError,
    fallback: SupervisorEvent,
) -> SupervisorEvent {
    match error {
        ProcessError::Protocol(ProtocolError::UnsupportedProtocolVersion { actual }) => {
            SupervisorEvent::ProtocolVersionMismatch { actual: *actual }
        }
        ProcessError::InitializeRejected { error_code } => SupervisorEvent::InitializeRejected {
            error_code: error_code.clone(),
        },
        _ => fallback,
    }
}

fn publish_error_if_active(
    state: &RuntimeStateHandle,
    supervisor: &mut Supervisor,
    error: &ProcessError,
    fallback: SupervisorEvent,
) {
    let event = supervisor_event_for_process_error(error, fallback);
    publish(state, supervisor.transition(event));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn broker_command_wakes_ready_runtime_without_polling_delay() {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("build paused-time runtime")
            .block_on(async {
                tokio::time::pause();
                let (_control_tx, mut control_rx) = tokio::sync::mpsc::unbounded_channel();
                let (broker_tx, mut broker_rx) = tokio::sync::mpsc::channel(1);
                let started_at = Instant::now();
                let waiter = tokio::spawn(async move {
                    wait_for_runtime_wake(
                        &mut control_rx,
                        &mut broker_rx,
                        std::future::pending(),
                        started_at + HEARTBEAT_INTERVAL,
                        started_at + HEARTBEAT_TIMEOUT,
                        true,
                    )
                    .await
                });

                tokio::task::yield_now().await;
                broker_tx
                    .send(RuntimeCommand::Shutdown)
                    .await
                    .expect("send broker shutdown");

                let wake = waiter.await.expect("runtime wake task");
                assert!(matches!(
                    wake,
                    RuntimeWake::Command(Some(RuntimeCommand::Shutdown))
                ));
                assert_eq!(Instant::now(), started_at);
            });
    }

    #[test]
    fn sidecar_output_wakes_ready_runtime_without_polling_delay() {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("build paused-time runtime")
            .block_on(async {
                tokio::time::pause();
                let (_control_tx, mut control_rx) = tokio::sync::mpsc::unbounded_channel();
                let (_broker_tx, mut broker_rx) = tokio::sync::mpsc::channel(1);
                let started_at = Instant::now();

                let wake = wait_for_runtime_wake(
                    &mut control_rx,
                    &mut broker_rx,
                    std::future::ready(Err(ProcessError::EventChannelClosed)),
                    started_at + HEARTBEAT_INTERVAL,
                    started_at + HEARTBEAT_TIMEOUT,
                    true,
                )
                .await;

                assert!(matches!(
                    wake,
                    RuntimeWake::Output(Err(ProcessError::EventChannelClosed))
                ));
                assert_eq!(Instant::now(), started_at);
            });
    }

    #[test]
    fn heartbeat_deadline_wakes_runtime_within_timer_precision() {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("build paused-time runtime")
            .block_on(async {
                tokio::time::pause();
                let (_control_tx, mut control_rx) = tokio::sync::mpsc::unbounded_channel();
                let (_broker_tx, mut broker_rx) = tokio::sync::mpsc::channel(1);
                let started_at = Instant::now();

                let wake = wait_for_runtime_wake(
                    &mut control_rx,
                    &mut broker_rx,
                    std::future::pending(),
                    started_at + HEARTBEAT_TIMEOUT + HEARTBEAT_INTERVAL,
                    started_at + HEARTBEAT_TIMEOUT,
                    true,
                )
                .await;

                assert!(matches!(wake, RuntimeWake::HeartbeatTimedOut));
                let woke_at = Instant::now();
                let deadline = started_at + HEARTBEAT_TIMEOUT;
                assert!(woke_at >= deadline);
                assert!(woke_at <= deadline + Duration::from_millis(1));
            });
    }
}
