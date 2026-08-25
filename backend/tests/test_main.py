def test_main_returns_success() -> None:
    try:
        from harness_shell_sidecar.__main__ import main
    except ModuleNotFoundError:
        raise AssertionError("sidecar entry point is missing") from None

    assert main() == 0
