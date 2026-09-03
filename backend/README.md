# Harness Shell Backend

Python Backend 提供仅监听 `127.0.0.1` 的 typed HTTP API 与单个 Runtime WebSocket。业务状态、凭据、SSH/PTY、remote Manual SFTP 和实验性 Agent 都由 Python 所有；Tauri 不代理这些调用。

源码开发必须显式指定端口与绝对数据目录：

```powershell
.\.venv\Scripts\python.exe -m harness_shell_sidecar serve --port 8765 --data-dir E:\absolute\harness-shell-dev
```

安装版只能由 `harness-shell-launcher.exe` 启动：

```text
harness-shell-sidecar.exe desktop --port 0 --data-dir <absolute> --control-read-handle <n> --ready-write-handle <n>
```

Desktop mode 绑定动态 loopback port，通过 inherited ready pipe 报告端口，并在 control pipe 收到 Launcher 的 graceful signal 后退出。它不扫描端口、不 reconnect、不 respawn。

Runtime SQLite 只接受全新 schema v6。旧 schema 不会迁移，并在修改文件前失败。schema v6 是 plaintext：credential、Agent message/output、remote recovery 与其他业务 payload 可能明文落盘。诊断信息只写日志目录，不再写 SQLite Audit/Trace 表。

构建与测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1
```

打包固定 Python 3.12.13 与 `x86_64-pc-windows-msvc`，依赖来自 `build-requirements.lock`。生成的 `build/`、`dist/` 与复制到 Tauri binaries 的 `.exe` 不得提交。测试和 packaged loopback smoke 不等于 Desktop、真实 Provider、生产 SSH 或部署验收。
