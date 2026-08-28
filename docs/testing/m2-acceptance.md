# M2 Windows Desktop Manual Acceptance

This checklist is separate from `scripts/verify-m2.ps1`. Record the Windows host, app build, start/end time, and every displayed `correlation_id`. Passing it proves only this checkout against the selected local/container hosts; it is not production-host acceptance.

## Evidence boundary

The automated gate proves this Windows checkout, the packaged Sidecar lifecycle contracts, Rust Vault contracts, and containerized OpenSSH behavior exercised directly through the Python runtime. The OpenSSH tests do not route lab credentials through the desktop Core/Vault process. They do not prove a production host, Provider, Agent workflow, L2 approval, sudo, or any remote write path.

## Record

- Tester: Codex Computer Use（用户授权的本机桌面验收）
- Windows build: Microsoft Windows 11 家庭版 中文版，10.0.26200（Build 26200）
- Harness Shell commit: `50b64c1b5ee2ace1ecb55336fc84c8d0ba989278` (`develop`)
- Start time (timezone): 2026-08-28 09:45:25 +08:00
- End time (timezone): 2026-08-28 09:53:18 +08:00
- Correlation IDs: 阻塞发生前 UI 未显示 correlation ID

### 2026-08-28 observed run

- Fresh automated gate: PASS。当前 Codex task 执行日志最终行为 `M2 automated gate passed: local Windows checkout plus containerized OpenSSH lab only.`；SSH integration 为 `8 passed`。
- Created the local Password profile `M2 Local Direct` for `jumpuser@127.0.0.1:2222` under group `M2 Acceptance` and exercised **Save & Connect**.
- First Host Key prompt was cancelled; the terminal remained `Disconnected` and the footer showed `Host Key: untrusted`.
- Reopened the profile, independently matched `127.0.0.1:2222`, algorithm `ssh-ed25519`, fingerprint `SHA256:n0EagpyCmmI3I3e6Km3a4L4XUOMxQ3F7+u0pma5Dh7Q` against the generated lab manifest, then used **Trust and connect**.
- The PTY reached `Connected` and displayed the remote Bash prompt, but the footer continued to show `Host Key: untrusted` after an additional refresh.
- Source tracing found the successful `confirmHostKey()` path establishes the Session without replacing the earlier candidate-bearing `connectionChecks` entry; `hostKeyTrustLabel(selectedCheck)` therefore projects stale `untrusted` state.
- This is a blocking contradictory Desktop projection. The run stopped immediately; ProxyJump, rotation, full PTY matrix, Sidecar-loss, shutdown, responsive/locale, disabled-control, persistence and multi-PTY checks were not executed and must not be inferred as passing.

### 2026-08-28 Host Key fix retry

- Tester: Codex Computer Use（用户授权的本机桌面复验）
- Windows build: Microsoft Windows 11 家庭版 中文版，10.0.26200（Build 26200）
- Harness Shell commit: `50b64c1b5ee2ace1ecb55336fc84c8d0ba989278` (`develop`) plus the uncommitted Host Key state fix and regression test listed by `git diff`.
- Start time (timezone): 2026-08-28 10:02:42 +08:00
- End time (timezone): 2026-08-28 10:11:24 +08:00
- Correlation IDs: 本轮 UI 未显示 correlation ID
- Fresh automated gate: PASS。`scripts/verify-m2.ps1` ended with `M2 automated gate passed: local Windows checkout plus containerized OpenSSH lab only.`；Python 为 `125 passed, 8 skipped`，Web 为 `170 passed`，SSH integration 为 `8 passed`，Vault runtime evidence 为 `1 passed`。
- Restarting the deterministic lab changed the direct endpoint Host Key from trusted fingerprint `SHA256:n0EagpyCmmI3I3e6Km3a4L4XUOMxQ3F7+u0pma5Dh7Q` to `SHA256:ULYiwtfLgtK5xXtJ4ZbiqVNDfPHWO7XWyEAE/d29Qnc`. The Desktop displayed host `127.0.0.1:2222`, algorithm `ssh-ed25519`, and both full fingerprints under **Host Key changed**.
- The new fingerprint exactly matched `tests/ssh_lab/.runtime/manifest.json` read independently before using **Replace trusted key**.
- After replacement, the PTY reached `Connected` and the footer projected `Runtime: READY`, `Host Key: trusted`, and `PTY size: 131×45`. This is the exact previously failing Desktop projection.
- Closing the Session and reopening the same Profile connected without another Host Key prompt and retained `Host Key: trusted`, proving the corrected state survives the normal reconnect path.
- Sidecar-loss check: with the direct PTY focused, terminating the PyInstaller launcher alone left its worker running; terminating the actual managed worker then changed the tab to `Disconnected`, runtime to `PAUSED`, and displayed `Sidecar unavailable. Existing PTYs are disconnected; no fallback terminal was created.` A process check returned `NO_SIDECAR_OR_SSH_PROCESS`.
- Application exit check after the injected Sidecar loss: **Exit Harness Shell** removed the Desktop window, the `tauri dev` process exited with code `0`, and a final process query returned no `harness-shell.exe`, `harness-shell-sidecar.exe`, or `ssh.exe`. This does not replace the still-required shutdown check with both direct and ProxyJump tabs open.
- Profile cleanup: PASS by user confirmation after the Desktop run; the local M2 test Profile was successfully deleted.
- Targeted Host Key defect reacceptance: PASS.
- Full M2 manual matrix: INCOMPLETE. Computer Use policy forbids automating terminal applications, so Bash/CJK/emoji/full-screen input and multi-PTY byte-isolation must be performed by the user or another approved manual tester. ProxyJump, both-endpoint rotation/conflict, full responsive/locale matrix, DevTools inspection, and shutdown with both direct and ProxyJump tabs remain unexecuted and must not be inferred as passing.

### 2026-08-28 full-matrix retry

- Tester: Codex Computer Use（用户授权继续复验）
- Windows build: Microsoft Windows 11 家庭版 中文版，10.0.26200（Build 26200）
- Harness Shell commit: `50b64c1b5ee2ace1ecb55336fc84c8d0ba989278` (`develop`) plus the uncommitted Host Key state fix and regression test listed by `git diff`.
- Start time (timezone): 2026-08-28 10:20:52 +08:00
- End time (timezone): 2026-08-28 10:26:53 +08:00
- Correlation IDs: 阻塞发生在 Profile 保存前，UI 未显示 correlation ID
- The deterministic OpenSSH lab and Harness Shell Desktop started successfully. A new Password profile was filled with name `M2 Direct`, group `M2 Acceptance`, host `127.0.0.1`, port `2222`, username `jumpuser`, and the generated lab password.
- After entering the password under **Authentication**, switching to **Advanced**, enabling **Favorite**, and pressing **Save**, the dialog returned to **Authentication** with `Password: Required`. No Profile was saved.
- Source tracing confirmed that the Password and Private Key passphrase inputs exist only inside the conditionally mounted Authentication panel. Their unsaved values are held only by DOM refs; switching tabs unmounts the inputs and clears the refs before Save validation reads them. The current dialog tests submit immediately after entering a secret and therefore do not cover Authentication → Advanced → Save.
- This is a blocking Profile-creation defect and likely affects both Password and Private Key passphrase flows. The run stopped immediately; Direct/ProxyJump connection, two-endpoint Host Key, rotation/conflict, remaining UI/security checks, user terminal checks, and two-session shutdown were not executed and must not be inferred as passing.
- Cleanup: the Desktop dev process was interrupted, the OpenSSH containers and networks were removed, the temporary Lab secrets were cleared from the automation session, and a final process query returned `NO_HARNESS_SIDECAR_OR_SSH_PROCESS`.
- Full M2 manual matrix: FAIL / INCOMPLETE pending separate authorization to repair the Connection Dialog secret-state defect and rerun acceptance.

### 2026-08-28 Connection Dialog and ProxyJump sequential Host Key fix retry

- Tester: Codex Computer Use（用户授权修复并继续复验）
- Windows build: Microsoft Windows 11 家庭版 中文版，10.0.26200（Build 26200）
- Harness Shell commit: `50b64c1b5ee2ace1ecb55336fc84c8d0ba989278` (`develop`) plus the uncommitted fixes and regression tests listed by `git diff`.
- Desktop run time: 本轮开始与结束时间未单独捕获，不补写推测值；最终自动化门禁结束于 2026-08-28 11:11:52 +08:00。
- Correlation IDs: 修复前 ProxyJump 复验显示 `3215473f-bbc2-4ca1-b934-13136dada727`；修复后未显示新的错误 correlation ID。
- Connection Dialog secret-state fix: Basic、Authentication、Advanced panel 保持挂载，仅用 `hidden` 切换可见性。新增 Password 与 encrypted Private Key passphrase 的 Authentication → Advanced → Save 回归测试。
- Desktop secret-state reacceptance: PASS。通过三个 tab 创建并编辑 `M2 Direct` Password Profile 和 `M2 Target Key` encrypted Private Key/passphrase Profile；切换到 Advanced 后 Save / Save & Connect 成功，重新编辑时密码与 passphrase 均未回填到输入框。
- Initial ProxyJump retry exposed a second blocking defect: after trusting the Jump Host key, the target key inspection returned `HOST_KEY_REQUIRED`, but the frontend immediately attempted connection and displayed `SSH_CONNECTION_NOT_READY` instead of presenting the target prompt.
- ProxyJump sequential Host Key fix: when the confirmed candidate belongs to the Jump Host, the frontend re-inspects the target Profile and surfaces its typed target candidate before any connection attempt. The regression test proves no early `connectSsh()` call and requires both confirmations.
- Rotated-lab Desktop reacceptance: the **Host Key changed** prompt for jump `127.0.0.1:2222` showed algorithm `ssh-ed25519` and new fingerprint `SHA256:xDFAA6GrkunpiKS3LFgmCOd+8A5efXwNRz65lInRe5M`, which exactly matched the generated manifest before **Replace trusted key**.
- Immediately after replacing the jump key, the Desktop displayed a distinct target prompt for `target:22`, algorithm `ssh-ed25519`, fingerprint `SHA256:nz13UEgBegFwHIg7rTSpKexYFjHquAy2Z8FbHktJ3IY`; it exactly matched the manifest. Trusting it connected without `SSH_CONNECTION_NOT_READY`.
- ProxyJump PTY reached `Connected`; the footer showed `Runtime: READY`, `Host Key: trusted`, `PTY size: 131×45`, and `Route: ProxyJump`.
- Concurrent-session and shutdown check: PASS for the observable Desktop/process scope. `M2 Target Key` and `M2 Direct` were simultaneously connected in separate tabs. Closing Harness Shell through **Exit Harness Shell** removed the window, `tauri dev` exited with code `0`, and the final process query returned no `harness-shell.exe`, `harness-shell-sidecar.exe`, or `ssh.exe`.
- Fresh final automated gate: PASS. `scripts/verify-m2.ps1` exited with code `0` and the explicit final line `M2 automated gate passed: local Windows checkout plus containerized OpenSSH lab only.` Python: `125 passed, 8 skipped`; Web: `173 passed`; production build: `119 modules transformed`; SSH integration: `8 passed`; Vault runtime evidence: `1 passed`.
- Tool-executable Desktop scope for both repaired defects: PASS.
- Full M2 manual matrix: INCOMPLETE. Computer Use policy forbids automating terminal input, so Bash/CJK/emoji/full-screen terminal input, hidden-tab typing, and multi-PTY byte isolation remain user/manual-tester checks. The unchecked responsive/locale, DevTools persistence/log/Trace/Audit, disabled-control, stale replacement-conflict, and remaining profile-management cases below also remain unproven. M3 must not begin yet.

### 2026-08-28 Computer Use UI-matrix retry

- Tester: Codex Computer Use（用户授权继续复验）
- Windows build: Microsoft Windows 11 家庭版 中文版，10.0.26200（Build 26200）
- Harness Shell commit: `50b64c1b5ee2ace1ecb55336fc84c8d0ba989278` (`develop`) plus the uncommitted fixes and regression tests listed by `git diff`.
- Desktop run time: 本轮开始时间未单独捕获，不补写推测值；验收记录结束于 2026-08-28 11:45:11 +08:00。
- Correlation IDs: 一次使用旧 Password secret 的预期连接失败显示 `7b475c4b-0aad-4956-9da0-bb00ddf7de0a`；原始错误字段保持为 `SSH_AUTHENTICATION_FAILED`、`authentication`、`recoverable=false`、`pre_auth`。
- Profile UI: search filtered `M2 Target Key` and clearing the query restored the full list. Editing `M2 Direct` showed an empty Password input with “留空以保留現有秘密”. Editing `M2 Target Key` showed the same non-replay contract for both Private Key and passphrase.
- Save-only: PASS. Pressing **Save** closed the edit dialog, retained the existing connected PTY, and left the `M2 Direct` terminal-tab count unchanged at three; no connection was started. **Save & Connect** separately closed the dialog and reached a connected Direct PTY after the fresh Lab password was supplied.
- Host Key rotation: the Direct prompt showed old fingerprint `SHA256:xDFAA6GrkunpiKS3LFgmCOd+8A5efXwNRz65lInRe5M` and new fingerprint `SHA256:MWpFSS3x22aJdU1qVLjNCwHbDHZIpV4u/pKEWSe6CYo`; the new value exactly matched the generated Lab manifest before **Replace trusted key**. The connected footer then showed `Host Key: trusted`. The stale-replacement conflict path remains untested.
- Locale: PASS for `system`, `zh-CN`, `zh-TW`, and `en`. Follow-system resolved to `zh-CN` on this host, manual `zh-TW` persisted across a full application exit/restart, and switching `zh-TW → en → zh-TW` with an active PTY preserved `Connected`, `trusted`, and `93×42`.
- Connection Explorer and dialog refit: PASS. Resizing the Explorer across its legal range preserved layout; expanding/collapsing/restoring it changed the active PTY `110×42 → 93×42 → 140×42 → 93×42`. Opening and closing the connection dialog with the PTY active preserved the connection and final `93×42` projection.
- Responsive layout: 1280×720 and 1100×720 were usable without clipping or unreachable controls, including the responsive Connections drawer. The configured window minimum is `minWidth: 960` and `minHeight: 640`, so the required 900×600 case cannot be reached. The observed minimum window (approximately 962×660 including borders) remained usable, but this is an acceptance requirement/configuration mismatch and therefore a FAIL for the stated 900×600 case.
- Milestone controls: Files and SFTP are exposed as disabled and a mouse click did not navigate. The Agent pane is visibly locked to M3. Enter/Space/shortcut exclusion was not conclusively completed. Settings is intentionally enabled for locale selection, contradicting the checklist sentence that currently classifies Settings as disabled.
- Full M2 manual matrix: FAIL / INCOMPLETE. The 900×600 requirement is unreachable under the current Tauri minimum size, DevTools secret/log/Trace/Audit inspection and stale Host Key replacement conflict remain unproven, and Computer Use policy forbids the remaining terminal-input and byte-isolation checks. M3 must not begin yet.

### 2026-08-28 user-completed manual acceptance

- Tester: 用户人工验收；结果由用户于 2026-08-28 12:19:54 +08:00 明确确认为通过。
- Responsive requirement decision: the required minimum viewport is revised from 900×600 to 960×640 so that the acceptance contract matches the configured Tauri `minWidth: 960` and `minHeight: 640`. The previously recorded 1280×720, 1100×720, and effective 960×640 Desktop layouts are accepted as PASS.
- Interactive PTY: PASS by user confirmation for Bash, UTF-8/CJK/emoji, alternate/full-screen terminal behavior, hidden-tab typing exclusion, focus survival, and multi-PTY byte isolation.
- Milestone and Agent isolation: PASS by user confirmation. Files and SFTP are disabled and skipped by keyboard focus; Settings remains enabled by design for locale selection; the Agent surface may expand/collapse but exposes no M2 Agent execution, remote read/hash, or SFTP command. No Files/SFTP shortcut is defined, so shortcut activation is not applicable.
- Secret persistence inspection: PASS by user confirmation. Password, Private Key, and passphrase markers were absent from localStorage, persisted Zustand state, application logs, Vault plaintext/metadata exposure, Trace attributes, and Audit bodies.
- Stale Host Key replacement: PASS by user confirmation; the compare-and-swap conflict path displayed `HOST_KEY_REPLACE_CONFLICT` and did not continue authentication from stale trust state.
- Full M2 manual matrix: PASS for this checkout against the selected local/container OpenSSH Lab, subject to the Evidence boundary above. This completes the M2 acceptance gate; beginning M3 implementation remains a separate authorization decision.

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

- [x] Verify 1280×720, 1100×720, and 960×640 with no overlap, clipping, or unreachable controls.
- [x] Resize the Connection Explorer through its legal range, collapse/restore it, and verify xterm refits after resize, dialog close, and locale change.
- [x] Switch system, zh-CN, zh-TW, and en modes; restart and record the persisted manual mode and current system-mode resolution.
- [x] Confirm Files and SFTP are disabled by mouse and skipped by keyboard focus; Settings remains enabled for locale selection, and the Agent surface exposes no M2 execution capability.
- [x] Create/edit Password and Private Key Profiles through all three tabs; verify secrets are absent from localStorage, logs, Trace, Audit, Vault plaintext/metadata exposure, and persisted Zustand state.
- [x] Verify Save closes without connecting; Save & Connect closes first and then enters Host Key inspection.
- [x] Force a post-save connection failure and record the localized partial-success summary plus unchanged raw error fields.
- [x] Open multiple PTYs; verify hidden tabs receive no typing and terminal focus/input survives sidebar, dialog, and language operations.

## Result

- Automated gate log attached: PASS in the current Codex task execution log (2026-08-28; Python `125 passed, 8 skipped`; Web `173 passed`; production build `119 modules transformed`; SSH integration `8 passed`; Vault runtime evidence `1 passed`).
- Targeted Host Key defect reacceptance: PASS.
- Connection Dialog cross-tab secret-state reacceptance: PASS.
- ProxyJump sequential Jump/target Host Key reacceptance: PASS.
- Direct plus ProxyJump simultaneous-session shutdown reacceptance: PASS for the observable Desktop/process scope.
- Locale, Explorer/dialog refit, Profile search, secret non-replay UI, Save-only, Save & Connect, and localized post-save failure checks: PASS for the recorded Computer Use scope.
- Responsive minimum: PASS at the user-approved 960×640 requirement, matching the configured Tauri minimum.
- Interactive terminal, multi-PTY isolation, milestone/Agent isolation, secret persistence inspection, and stale Host Key replacement conflict: PASS by user manual confirmation.
- Full manual result: PASS.
- Deviations or unresolved risks: the earlier contradictory `Host Key: untrusted` projection, cross-tab secret loss, missing second ProxyJump Host Key prompt, and 900×600 acceptance mismatch remain preserved above as historical failures and have been resolved or superseded by the recorded fixes and explicit requirement decision. No unresolved M2 manual acceptance item remains for the current checkout/local Lab scope. M3 implementation requires separate user authorization.
