from __future__ import annotations

import asyncio
import base64
from uuid import uuid4

from harness_shell_sidecar.manual_sftp.service import ManualSftpService
from harness_shell_sidecar.terminal.manager import PtyManager

from .test_manual_sftp import _download, _ignore_event, _upload, _write_event_evidence


def test_pty_and_manual_sftp_use_isolated_child_channels(
    runtime_context,
    connect_proxy,
) -> None:
    async def scenario() -> None:
        _jump, _target, status = await connect_proxy()
        assert status.session_id is not None
        session_id = status.session_id
        pty_events: list[dict] = []
        pty_output = asyncio.Event()

        async def collect_pty(payload: dict) -> None:
            """Capture only typed PTY events and wake after visible output."""

            pty_events.append(payload)
            _write_event_evidence(payload)
            if payload.get("event") == "ssh.pty.output":
                pty_output.set()

        pty = PtyManager(
            runtime_context.runtime.sessions,
            event_listener=collect_pty,
        )
        service = ManualSftpService(
            runtime_context.runtime.sessions,
            runtime_context.records,
            _ignore_event,
        )
        context = await service.open(session_id)
        remote_path = f"{context.home}/manual-sftp-isolation-{uuid4().hex}.txt"
        pty_session = await pty.open(session_id, cols=80, rows=24)
        try:
            await pty.write(pty_session.pty_session_id, b"printf 'pty-visible-only\\n'\n")
            await asyncio.wait_for(pty_output.wait(), timeout=5)

            await _upload(service, session_id, remote_path, b"manual-sftp-payload")
            downloaded_payload = await _download(service, session_id, remote_path)
            remote_hash = await service.sha256(session_id, remote_path)
            await service.remove(
                operation_id=uuid4(),
                ssh_session_id=session_id,
                path=remote_path,
                expected_snapshot=remote_hash.snapshot,
            )

            pty_transcript = b"".join(
                base64.b64decode(event["data_b64"], validate=True)
                for event in pty_events
                if event.get("event") == "ssh.pty.output"
            )
            assert b"manual-sftp-payload" not in pty_transcript
            assert downloaded_payload == b"manual-sftp-payload"
            assert len(runtime_context.runtime.sessions) == 1
            owner = runtime_context.runtime.sessions.get(session_id)
            assert owner is not None
            assert len(owner.child_channels) == 1
        finally:
            await service.close_all()
            await pty.close(pty_session.pty_session_id)

    async def bounded_scenario() -> None:
        """Fail the channel-isolation scenario instead of allowing an unbounded hang."""

        try:
            async with asyncio.timeout(30):
                await scenario()
        finally:
            async with asyncio.timeout(10):
                await runtime_context.close()

    asyncio.run(bounded_scenario())
