use std::{
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant},
};

use harness_shell_launcher::{
    config::LauncherConfig,
    control::ReadyFrame,
    error::LauncherError,
    run,
};
use tempfile::TempDir;
use windows_sys::Win32::{
    Foundation::{CloseHandle, WAIT_OBJECT_0},
    System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE},
};

static ENVIRONMENT_LOCK: Mutex<()> = Mutex::new(());

#[test]
fn ready_frame_rejects_unknown_fields_and_zero_port() {
    assert!(ReadyFrame::decode(
        br#"{"version":1,"instance_id":"01234567-89ab-4def-8123-456789abcdef","port":0}"#,
    )
    .is_err());
    assert!(ReadyFrame::decode(
        br#"{"version":1,"instance_id":"01234567-89ab-4def-8123-456789abcdef","port":49152,"extra":1}"#,
    )
    .is_err());
    assert!(ReadyFrame::decode(
        br#"{"version":1,"version":1,"instance_id":"01234567-89ab-4def-8123-456789abcdef","port":49152}"#,
    )
    .is_err());
}

#[test]
fn installed_paths_are_fixed_siblings() {
    let config = LauncherConfig::from_executable(Path::new(
        r"C:\App\harness-shell-launcher.exe",
    ))
    .unwrap();
    assert_eq!(config.ui_exe, Path::new(r"C:\App\harness-shell-ui.exe"));
    assert_eq!(
        config.backend_exe,
        Path::new(r"C:\App\harness-shell-sidecar.exe"),
    );
}

#[test]
fn child_arguments_are_exact_and_do_not_expose_extra_state() {
    let mut config = LauncherConfig::from_executable(Path::new(
        r"C:\App\harness-shell-launcher.exe",
    ))
    .unwrap();
    config.data_dir = r"C:\Users\Example\AppData\Local\com.harnessshell.app".into();

    assert_eq!(
        config.backend_arguments(164, 168),
        vec![
            "desktop",
            "--port",
            "0",
            "--data-dir",
            r"C:\Users\Example\AppData\Local\com.harnessshell.app",
            "--control-read-handle",
            "164",
            "--ready-write-handle",
            "168",
        ]
    );
    assert_eq!(
        LauncherConfig::ui_arguments(49_152),
        vec!["--backend-url", "http://127.0.0.1:49152"],
    );
}

#[test]
fn process_is_assigned_to_the_job_before_its_primary_thread_resumes() {
    let source = fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("src/process.rs"),
    )
    .unwrap();
    assert!(source.contains("PROC_THREAD_ATTRIBUTE_HANDLE_LIST"));
    let assignment = source.find("job.assign(raw(&process))").unwrap();
    let resume = source.find("ResumeThread(raw(&thread))").unwrap();
    assert!(assignment < resume);
}

#[test]
fn malformed_and_oversized_ready_frames_fail_closed() {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    for mode in ["malformed", "oversized"] {
        let fixture = InstalledFixture::new();
        let _environment = TestEnvironment::new(&[
            ("HARNESS_LAUNCHER_TEST_BACKEND_MODE", mode.into()),
            ("HARNESS_LAUNCHER_TEST_UI_MODE", "exit-immediately".into()),
        ]);
        assert_eq!(run(fixture.config), Err(LauncherError::BackendReadyFailed));
    }
}

#[test]
fn backend_exit_before_readiness_never_starts_the_ui() {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    let fixture = InstalledFixture::new();
    let ui_arguments = fixture.root.path().join("ui-arguments.txt");
    let _environment = TestEnvironment::new(&[
        ("HARNESS_LAUNCHER_TEST_BACKEND_MODE", "early-exit".into()),
        ("HARNESS_LAUNCHER_TEST_UI_ARGUMENTS", ui_arguments.clone().into_os_string()),
    ]);

    assert_eq!(run(fixture.config), Err(LauncherError::BackendExitedEarly));
    assert!(!ui_arguments.exists());
}

#[test]
fn backend_stderr_is_written_to_the_dedicated_log_file() {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    let fixture = InstalledFixture::new();
    let log_path = fixture
        .config
        .data_dir
        .join("logs")
        .join("harness-shell-backend.log");
    let _environment = TestEnvironment::new(&[
        ("HARNESS_LAUNCHER_TEST_BACKEND_MODE", "ready-wait".into()),
        ("HARNESS_LAUNCHER_TEST_UI_MODE", "exit-immediately".into()),
        (
            "HARNESS_LAUNCHER_TEST_BACKEND_STDERR",
            "backend-log-marker".into(),
        ),
    ]);

    assert_eq!(run(fixture.config), Ok(()));
    assert_eq!(fs::read(log_path).unwrap(), b"backend-log-marker\n");
}

#[test]
fn full_backend_log_rotates_before_new_stderr_is_appended() {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    let fixture = InstalledFixture::new();
    let log_dir = fixture.config.data_dir.join("logs");
    fs::create_dir_all(&log_dir).unwrap();
    let log_path = log_dir.join("harness-shell-backend.log");
    fs::write(&log_path, vec![b'x'; 10 * 1024 * 1024]).unwrap();
    let _environment = TestEnvironment::new(&[
        ("HARNESS_LAUNCHER_TEST_BACKEND_MODE", "ready-wait".into()),
        ("HARNESS_LAUNCHER_TEST_UI_MODE", "exit-immediately".into()),
        (
            "HARNESS_LAUNCHER_TEST_BACKEND_STDERR",
            "after-rotation".into(),
        ),
    ]);

    assert_eq!(run(fixture.config), Ok(()));
    assert_eq!(fs::read(&log_path).unwrap(), b"after-rotation\n");
    assert_eq!(
        fs::metadata(log_path.with_extension("log.1"))
            .unwrap()
            .len(),
        10 * 1024 * 1024
    );
}

#[test]
fn ui_exit_sends_one_graceful_byte_and_uses_the_ready_port() {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    let fixture = InstalledFixture::new();
    let shutdown_marker = fixture.root.path().join("shutdown.txt");
    let ui_arguments = fixture.root.path().join("ui-arguments.txt");
    let _environment = TestEnvironment::new(&[
        ("HARNESS_LAUNCHER_TEST_BACKEND_MODE", "ready-wait".into()),
        ("HARNESS_LAUNCHER_TEST_UI_MODE", "exit-immediately".into()),
        ("HARNESS_LAUNCHER_TEST_SHUTDOWN_MARKER", shutdown_marker.clone().into_os_string()),
        ("HARNESS_LAUNCHER_TEST_UI_ARGUMENTS", ui_arguments.clone().into_os_string()),
    ]);

    assert_eq!(run(fixture.config), Ok(()));
    assert_eq!(fs::read(&shutdown_marker).unwrap(), b"graceful");
    let arguments = fs::read_to_string(ui_arguments).unwrap();
    assert!(arguments.contains("--backend-url"));
    assert!(arguments.contains("http://127.0.0.1:49152"));
    assert!(!arguments.contains("control-read-handle"));
    assert!(!arguments.contains("ready-write-handle"));
}

#[test]
fn graceful_timeout_terminates_the_backend_job_tree() {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    let fixture = InstalledFixture::new();
    let grandchild_pid = fixture.root.path().join("grandchild.pid");
    let _environment = TestEnvironment::new(&[
        ("HARNESS_LAUNCHER_TEST_BACKEND_MODE", "ignore-shutdown".into()),
        ("HARNESS_LAUNCHER_TEST_UI_MODE", "exit-immediately".into()),
        ("HARNESS_LAUNCHER_TEST_SPAWN_GRANDCHILD", "1".into()),
        ("HARNESS_LAUNCHER_TEST_GRANDCHILD_PID", grandchild_pid.clone().into_os_string()),
    ]);

    let started = Instant::now();
    assert_eq!(run(fixture.config), Ok(()));
    assert!(started.elapsed() >= Duration::from_secs(3));
    let pid = fs::read_to_string(grandchild_pid)
        .unwrap()
        .parse::<u32>()
        .unwrap();
    assert!(process_has_exited(pid));
}

#[test]
fn backend_exit_after_readiness_does_not_respawn_or_close_the_ui() {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    let fixture = InstalledFixture::new();
    let _environment = TestEnvironment::new(&[
        ("HARNESS_LAUNCHER_TEST_BACKEND_MODE", "ready-exit-delay".into()),
        ("HARNESS_LAUNCHER_TEST_BACKEND_DELAY_MS", "200".into()),
        ("HARNESS_LAUNCHER_TEST_UI_MODE", "wait".into()),
        ("HARNESS_LAUNCHER_TEST_UI_DELAY_MS", "500".into()),
    ]);

    let started = Instant::now();
    assert_eq!(run(fixture.config), Ok(()));
    assert!(started.elapsed() >= Duration::from_millis(450));
}

struct InstalledFixture {
    root: TempDir,
    config: LauncherConfig,
}

impl InstalledFixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let source = PathBuf::from(env!("CARGO_BIN_EXE_launcher_test_child"));
        let ui_exe = root.path().join("harness-shell-ui.exe");
        let backend_exe = root.path().join("harness-shell-sidecar.exe");
        fs::copy(&source, &ui_exe).unwrap();
        fs::copy(&source, &backend_exe).unwrap();
        let config = LauncherConfig {
            ui_exe,
            backend_exe,
            data_dir: root.path().join("data"),
        };
        Self { root, config }
    }
}

struct TestEnvironment {
    names: Vec<&'static str>,
}

impl TestEnvironment {
    fn new(values: &[(&'static str, std::ffi::OsString)]) -> Self {
        let names = values.iter().map(|(name, _)| *name).collect::<Vec<_>>();
        for (name, value) in values {
            std::env::set_var(name, value);
        }
        Self { names }
    }
}

impl Drop for TestEnvironment {
    fn drop(&mut self) {
        for name in &self.names {
            std::env::remove_var(name);
        }
    }
}

fn process_has_exited(process_id: u32) -> bool {
    let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, process_id) };
    if handle.is_null() {
        return true;
    }
    let result = unsafe { WaitForSingleObject(handle, 1_000) };
    unsafe { CloseHandle(handle) };
    result == WAIT_OBJECT_0
}
