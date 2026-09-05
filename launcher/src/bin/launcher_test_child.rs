use std::{
    fs::File,
    io::{Read, Write},
    os::windows::io::FromRawHandle,
    process::Command,
    time::Duration,
};

use serde_json::json;

fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    if arguments.iter().any(|argument| argument == "--grandchild") {
        wait_forever();
    }
    let executable = std::env::current_exe().expect("test child executable path");
    let name = executable
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    if name.contains("sidecar") {
        run_backend(&arguments);
    } else {
        run_ui(&arguments);
    }
}

fn run_backend(arguments: &[String]) {
    let mode = std::env::var("HARNESS_LAUNCHER_TEST_BACKEND_MODE")
        .unwrap_or_else(|_| "ready-wait".to_owned());
    if let Some(message) = std::env::var_os("HARNESS_LAUNCHER_TEST_BACKEND_STDERR") {
        writeln!(std::io::stderr(), "{}", message.to_string_lossy())
            .expect("write Backend stderr marker");
    }
    if mode == "early-exit" {
        std::process::exit(7);
    }
    let control = argument(arguments, "--control-read-handle");
    let ready = argument(arguments, "--ready-write-handle");
    if std::env::var_os("HARNESS_LAUNCHER_TEST_SPAWN_GRANDCHILD").is_some() {
        let child = Command::new(std::env::current_exe().unwrap())
            .arg("--grandchild")
            .spawn()
            .expect("spawn test grandchild");
        if let Some(path) = std::env::var_os("HARNESS_LAUNCHER_TEST_GRANDCHILD_PID") {
            std::fs::write(path, child.id().to_string()).expect("write grandchild PID");
        }
    }

    let payload = match mode.as_str() {
        "malformed" => b"{not-json".to_vec(),
        "oversized" => vec![b'x'; 4_097],
        _ => serde_json::to_vec(&json!({
            "instance_id": "01234567-89ab-4def-8123-456789abcdef",
            "port": 49_152,
            "version": 1,
        }))
        .unwrap(),
    };
    let mut ready = unsafe { File::from_raw_handle(ready as _) };
    ready
        .write_all(&(payload.len() as u32).to_be_bytes())
        .expect("write ready prefix");
    ready.write_all(&payload).expect("write ready payload");
    ready.flush().expect("flush ready frame");
    drop(ready);

    if mode == "ready-exit-delay" {
        let delay = std::env::var("HARNESS_LAUNCHER_TEST_BACKEND_DELAY_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(100);
        std::thread::sleep(Duration::from_millis(delay));
        return;
    }
    if mode == "ignore-shutdown" {
        wait_forever();
    }
    let mut control = unsafe { File::from_raw_handle(control as _) };
    let mut byte = [0u8; 1];
    let read = control.read(&mut byte).expect("read shutdown control");
    if read == 1 && byte != [0x01] {
        std::process::exit(9);
    }
    if let Some(path) = std::env::var_os("HARNESS_LAUNCHER_TEST_SHUTDOWN_MARKER") {
        std::fs::write(path, b"graceful").expect("write graceful shutdown marker");
    }
}

fn run_ui(arguments: &[String]) {
    if let Some(path) = std::env::var_os("HARNESS_LAUNCHER_TEST_UI_ARGUMENTS") {
        std::fs::write(path, arguments.join("\n")).expect("write UI arguments");
    }
    let mode = std::env::var("HARNESS_LAUNCHER_TEST_UI_MODE")
        .unwrap_or_else(|_| "exit-immediately".to_owned());
    if mode == "wait" {
        let delay = std::env::var("HARNESS_LAUNCHER_TEST_UI_DELAY_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(250);
        std::thread::sleep(Duration::from_millis(delay));
    }
}

fn argument(arguments: &[String], name: &str) -> usize {
    let index = arguments
        .iter()
        .position(|argument| argument == name)
        .expect("required test child argument");
    arguments[index + 1].parse().expect("numeric handle")
}

fn wait_forever() -> ! {
    loop {
        std::thread::sleep(Duration::from_secs(60));
    }
}
