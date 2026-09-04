from __future__ import annotations

from pathlib import Path

import pytest

from harness_shell_sidecar import __main__


def test_main_parses_only_explicit_serve_port_and_orders_process_setup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        __main__,
        "configure_stderr_logging",
        lambda *, colorize: calls.append(("configure", colorize)),
    )
    monkeypatch.setattr(
        __main__.LOGGER,
        "info",
        lambda event: calls.append((20, event)),
    )
    monkeypatch.setattr(
        __main__,
        "serve",
        lambda *, port, data_dir: calls.append(("serve", port, data_dir)) or 23,
    )

    assert __main__.main(
        ["serve", "--port", "43123", "--data-dir", str(tmp_path)]
    ) == 23
    assert calls == [
        ("configure", True),
        (20, "sidecar_process_started"),
        ("serve", 43123, tmp_path),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["serve"],
        ["serve", "--port", "0"],
        ["serve", "--port", "65536"],
        ["serve", "--port", "43123", "--data-dir", "relative"],
        ["serve", "--port", "43123", "--data-dir", "C:\\runtime", "extra"],
        ["serve", "--host", "127.0.0.1", "--port", "43123"],
        ["desktop", "--port", "1"],
        ["stdio"],
    ],
)
def test_main_rejects_missing_extra_or_legacy_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        __main__.main(argv)

    assert exited.value.code != 0


def test_main_passes_desktop_control_handles_to_server(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        __main__,
        "configure_stderr_logging",
        lambda *, colorize: calls.append(("configure", colorize)),
    )
    monkeypatch.setattr(__main__.LOGGER, "info", lambda *_args: None)
    monkeypatch.setattr(
        __main__,
        "desktop",
        lambda **kwargs: calls.append(kwargs) or 0,
    )

    assert __main__.main(
        [
            "desktop",
            "--port",
            "0",
            "--data-dir",
            str(tmp_path),
            "--control-read-handle",
            "101",
            "--ready-write-handle",
            "202",
        ]
    ) == 0
    assert calls == [
        ("configure", False),
        {
            "port": 0,
            "data_dir": tmp_path,
            "control_read_handle": 101,
            "ready_write_handle": 202,
        },
    ]
