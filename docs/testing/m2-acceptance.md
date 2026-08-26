# M2 Windows Desktop Manual Acceptance

This checklist is separate from `scripts/verify-m2.ps1`. Record the Windows host, app build, start/end time, and every displayed `correlation_id`. Passing it proves only this checkout against the selected local/container hosts; it is not production-host acceptance.

## Evidence boundary

The automated gate proves this Windows checkout, the packaged Sidecar lifecycle contracts, Rust Vault contracts, and containerized OpenSSH behavior exercised directly through the Python runtime. The OpenSSH tests do not route lab credentials through the desktop Core/Vault process. They do not prove a production host, Provider, Agent workflow, L2 approval, sudo, or any remote write path.

## Record

- Tester:
- Windows build:
- Harness Shell commit:
- Start time (timezone):
- End time (timezone):
- Correlation IDs:

## Checklist

1. Connection profiles
   - Create, edit, group, search, favorite, and delete direct and ProxyJump profiles.
   - Confirm password/passphrase inputs clear after submit and no plaintext appears in React state, DevTools storage, logs, Vault metadata, Trace, or Audit bodies.
2. Two-endpoint Host Key trust
   - Connect through the lab jump to the target.
   - Record the exact host, port, algorithm, and full SHA-256 fingerprint shown separately for jump and target.
   - Cancel once and confirm the connection remains disconnected; then explicitly trust both.
3. Host Key rotation
   - Rotate the target key and then the jump key.
   - For each endpoint, record the old and new full fingerprints and prove connection hard-fails before authentication.
   - Use only the separately labeled **Replace trusted key** action; stale replacement must display `HOST_KEY_REPLACE_CONFLICT`.
4. Interactive PTY
   - Open two terminal tabs and run Bash, UTF-8/CJK/emoji, and a full-screen terminal command.
   - Resize and close each tab independently; confirm bytes never appear in the other tab.
5. Agent isolation
   - Confirm the right panel states: “M2: Agent 尚未启用；Agent channel 与此人工 PTY 隔离”.
   - Inspect the DevTools invoke surface and confirm no Agent exec, remote read/hash, or SFTP command is exposed.
6. Sidecar loss
   - Kill the managed Sidecar while a PTY is focused.
   - Confirm every tab stops accepting input, runtime becomes `PAUSED`, and no fallback `ssh.exe` or replacement Sidecar process appears.
7. Application shutdown
   - Close Harness Shell with direct and ProxyJump tabs open.
   - Confirm all target, jump, PTY, exec/SFTP channels, and the Sidecar process terminate.

## Terminal-first UI redesign

- [ ] Verify 1280×720, 1100×720, and 900×600 with no overlap, clipping, or unreachable controls.
- [ ] Resize the Connection Explorer through its legal range, collapse/restore it, and verify xterm refits after resize, dialog close, and locale change.
- [ ] Switch system, zh-CN, zh-TW, and en modes; restart and record the persisted manual mode and current system-mode resolution.
- [ ] Confirm Files, SFTP, Settings, and Agent milestone controls are disabled by mouse, Enter, Space, and shortcuts.
- [ ] Create/edit Password and Private Key Profiles through all three tabs; verify secrets are absent from localStorage, logs, Trace, Audit, and persisted Zustand state.
- [ ] Verify Save closes without connecting; Save & Connect closes first and then enters Host Key inspection.
- [ ] Force a post-save connection failure and record the localized partial-success summary plus unchanged raw error fields.
- [ ] Open multiple PTYs; verify hidden tabs receive no typing and terminal focus/input survives sidebar, dialog, and language operations.

## Result

- Automated gate log attached:
- Manual result: PASS / FAIL
- Deviations or unresolved risks:
