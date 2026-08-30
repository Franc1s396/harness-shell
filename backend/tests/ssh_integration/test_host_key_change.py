from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from harness_shell_sidecar.ssh.errors import SshRuntimeError


LAB_ROOT = Path(__file__).resolve().parents[3] / "tests" / "ssh_lab"
ENV_FILE = LAB_ROOT / ".runtime" / "lab.env"


def compose_exec(service: str, script: str) -> None:
    subprocess.run(
        [
            "docker-compose.exe",
            "--env-file",
            str(ENV_FILE),
            "--project-name",
            "harness-shell-m2",
            "exec",
            "-T",
            service,
            "sh",
            "-eu",
            "-c",
            script,
        ],
        cwd=LAB_ROOT,
        check=True,
        capture_output=True,
    )
    time.sleep(0.5)


def rotate(service: str) -> None:
    compose_exec(
        service,
        "rm -f /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key.pub; "
        "ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key; "
        "kill -HUP 1",
    )


def restore(service: str) -> None:
    compose_exec(
        service,
        "cp /runtime/host_ed25519_key /etc/ssh/ssh_host_ed25519_key; "
        "cp /runtime/host_ed25519_key.pub /etc/ssh/ssh_host_ed25519_key.pub; "
        "chmod 0600 /etc/ssh/ssh_host_ed25519_key; kill -HUP 1",
    )


def test_target_and_jump_host_key_changes_hard_fail(
    runtime_context, connect_proxy, lab
) -> None:
    async def scenario() -> None:
        jump, target, status = await connect_proxy()
        await runtime_context.runtime.disconnect(status.session_id)
        rotate("target")
        try:
            changed_target = await runtime_context.runtime.inspect_host_key(
                target.connection_id,
                jump_connection_id=jump.connection_id,
                expected_jump_profile_version=jump.version,
                jump_password=lab.jump_password.encode(),
            )
            assert changed_target.state == "FAILED"
            assert changed_target.error_code == "HOST_KEY_CHANGED"
            assert changed_target.trusted_fingerprint_sha256 == lab.target_host_fingerprint
            assert changed_target.host_key_candidate.fingerprint_sha256 != lab.target_host_fingerprint
        finally:
            restore("target")

        rotate("jump")
        try:
            with pytest.raises(SshRuntimeError) as changed_jump:
                await runtime_context.runtime.inspect_host_key(
                    target.connection_id,
                    jump_connection_id=jump.connection_id,
                    expected_jump_profile_version=jump.version,
                    jump_password=lab.jump_password.encode(),
                )
            assert changed_jump.value.error_code == "HOST_KEY_CHANGED"
            assert changed_jump.value.remote_state == "pre_auth"
        finally:
            restore("jump")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime_context.close())
