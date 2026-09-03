# Testing Guide

## 证据分层

验证结论必须按层陈述：focused test、subsystem suite、packaged executable、Frontend build、Rust contract、containerized OpenSSH Lab、NSIS build、Desktop 人工验收、真实 Provider、生产 SSH/部署。低层通过不能替代高层验收。

## 真源与命令

- Python tests：`backend/tests/`
- Frontend tests：与 `frontend/src/` 模块相邻的 `*.test.ts(x)`
- Tauri shell tests：`frontend/src-tauri/tests/`
- Launcher tests：`launcher/tests/`
- SSH Lab：`tests/ssh_lab/` 与 `backend/tests/ssh_integration/`
- repository gates：`scripts/verify-*.ps1`

最小完整回归：

```powershell
backend\.venv\Scripts\python.exe -m pytest backend -q
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets --offline
cargo test --manifest-path launcher\Cargo.toml --all-targets --offline
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-installer-entry.ps1
```

仓库门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

## 门禁边界

- M1：本地 Windows Python/Frontend/Rust tests、packaged Backend 显式 `serve` loopback smoke、最小 Tauri capabilities 与 Frontend build。
- M2：M1 加 OpenSSH Lab 脚本/真实 SSH integration、plaintext schema-v6 evidence 与生成物跟踪检查。
- Manual SFTP：M2 加浏览器本地文件/hash/256 KiB raw-chunk contract、Python remote recovery 和真实 OpenSSH SFTP/PTY isolation。
- M3 Agent：Manual SFTP gate 加 Python `CredentialRepository` ownership、fake ChatModels 与 bound-session OpenSSH command。
- `verify-installer-entry.ps1`：只静态证明 NSIS input/shortcut/finish target；不证明安装或进程行为。

Python-only 与 SSH Lab 使用显式 `serve --port <fixed> --data-dir <isolated absolute>`。安装版 Desktop 只能从 Launcher 开始，并从 ready pipe 获得端口；测试和脚本不得扫描端口或直接把 UI/Backend 当用户入口。

## 必测契约

- schema v6 新建、自检、旧 schema 在任何写入前拒绝、plaintext record；不得重新出现无读取闭环的 Audit/Trace/Artifact 表。
- credential request envelope、Python repository kind match、Provider key lookup、secret non-logging。
- direct HTTP Problem、request ID、size/media/header/unknown-field failure。
- Runtime WebSocket single owner、ping/pong causation、queue、close、PTY input/event。
- Manual SFTP React picker/handle/hash/chunk loop和 Python remote temp/commit/abort/recovery。
- Launcher ready/control frame、handle inheritance、Job cleanup、UI-first/Backend-first exit、无 respawn。
- Tauri production bootstrap 只有 `get_backend_bootstrap`；main capability 只含 bootstrap 与固定 close/destroy 权限。

## Desktop 与安装验收

`scripts/build-desktop.ps1` 生成 NSIS 后，必须在 disposable Windows user profile 人工核对：只有一个 Harness Shell 用户入口、Launcher→Backend-ready→UI 顺序、direct HTTP/WebSocket、upload picker、同步 save picker、strict chunks、reload 丢失 local preparation、UI close graceful exit、forced cleanup 和无残留进程。

没有执行这组观察时，只能报告构建或静态检查，不能报告 Desktop/install acceptance。fake Provider 不是真实 Provider；containerized OpenSSH 不是生产 SSH。

## 生成物

不得提交 `.venv/`、`node_modules/`、`target/`、`dist/`、`build/`、`.runtime/`、SQLite、private key、Sidecar/Launcher companion `.exe`、Tauri bundle 或 generated schema。任务结束运行 `git diff --check` 并检查相关 AGENTS 文档影响。
