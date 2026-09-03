use harness_shell_lib::commands::BackendBootstrap;

#[test]
fn production_bootstrap_accepts_only_http_loopback_with_nonzero_port() {
    let value = BackendBootstrap::parse("http://127.0.0.1:49152").unwrap();
    assert_eq!(value.backend_base_url, "http://127.0.0.1:49152");
    assert!(BackendBootstrap::parse("http://localhost:49152").is_err());
    assert!(BackendBootstrap::parse("https://127.0.0.1:49152").is_err());
    assert!(BackendBootstrap::parse("http://127.0.0.1:0").is_err());
    assert!(BackendBootstrap::parse("http://127.0.0.1:49152/other").is_err());
}

#[test]
fn bootstrap_argument_must_be_unique_and_complete() {
    let parsed = BackendBootstrap::from_args([
        "harness-shell-ui.exe",
        "--backend-url",
        "http://127.0.0.1:49152",
    ])
    .unwrap()
    .expect("backend URL should be present");
    assert_eq!(parsed.backend_base_url, "http://127.0.0.1:49152");

    assert!(BackendBootstrap::from_args([
        "harness-shell-ui.exe",
        "--backend-url",
        "http://127.0.0.1:49152",
        "--backend-url",
        "http://127.0.0.1:49153",
    ])
    .is_err());
    assert!(BackendBootstrap::from_args([
        "harness-shell-ui.exe",
        "--backend-url",
    ])
    .is_err());
}
