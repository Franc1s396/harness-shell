from __future__ import annotations

import pytest

from harness_shell_sidecar import __main__


def test_main_parses_only_explicit_serve_port_and_orders_process_setup(
    monkeypatch,
) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        __main__,
        "configure_stderr_logging",
        lambda: calls.append("configure"),
    )
    monkeypatch.setattr(
        __main__,
        "log_event",
        lambda _logger, level, event: calls.append((level, event)),
    )
    monkeypatch.setattr(
        __main__,
        "serve",
        lambda *, port: calls.append(("serve", port)) or 23,
    )

    assert __main__.main(["serve", "--port", "43123"]) == 23
    assert calls == [
        "configure",
        (20, "sidecar_process_started"),
        ("serve", 43123),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["serve"],
        ["serve", "--port", "0"],
        ["serve", "--port", "65536"],
        ["serve", "--port", "43123", "extra"],
        ["serve", "--host", "127.0.0.1", "--port", "43123"],
        ["stdio"],
    ],
)
def test_main_rejects_missing_extra_or_legacy_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        __main__.main(argv)

    assert exited.value.code != 0


def test_frozen_main_attaches_required_job_before_server_setup(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(__main__.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        __main__, "attach_required_job", lambda: calls.append("job")
    )
    monkeypatch.setattr(
        __main__,
        "configure_stderr_logging",
        lambda: calls.append("configure"),
    )
    monkeypatch.setattr(__main__, "log_event", lambda *_args: None)
    monkeypatch.setattr(__main__, "serve", lambda *, port: 0)

    assert __main__.main(["serve", "--port", "43123"]) == 0
    assert calls == ["job", "configure"]
