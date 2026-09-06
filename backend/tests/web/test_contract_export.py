from __future__ import annotations

import subprocess
import sys


def test_exported_http_and_websocket_contracts_match_current_application() -> None:
    """要求已审查产物等于确定性生成字节。"""

    completed = subprocess.run(
        [sys.executable, "backend/scripts/export_http_contract.py", "--check"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr


def test_contract_export_is_deterministic_across_two_fresh_processes() -> None:
    """拒绝进程顺序或哈希顺序引起的跨语言产物偏差。"""

    command = [
        sys.executable,
        "backend/scripts/export_http_contract.py",
        "--stdout",
    ]
    first = subprocess.run(command, check=True, capture_output=True)
    second = subprocess.run(command, check=True, capture_output=True)

    assert first.stdout == second.stdout
