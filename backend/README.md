# Harness Shell Sidecar

Python Sidecar 是由 Tauri Rust Core 独占管理的本地子进程。唯一启动形式是 `harness-shell-sidecar.exe serve --port <1..65535>`；Uvicorn 固定监听 `127.0.0.1`，提供 `/v1/...` typed HTTP API 与单个 `/v1/runtime/events` WebSocket，不支持远程监听、认证、TLS、generic RPC 或兼容 transport。

初始化要求绝对路径的 runtime SQLite 数据库、两个 canonical base64 32-byte 密钥、5 秒 heartbeat 和 15 秒 timeout。启动会执行 schema migration、Audit HMAC chain 验证和本地存储 self-check；Audit 篡改会返回 `AUDIT_CHAIN_INVALID` 并在 `READY` 前退出。

运行时数据库只保存 AES-GCM 密文、allowlist Trace 属性和 append-only Audit。stdout 必须保持为空；结构化日志写 stderr，具体调用点不得提交密钥、credential、request/response body、SFTP bytes 或持久化明文。

本地源码调试入口必须显式指定端口：

```powershell
.\.venv\Scripts\python.exe -m harness_shell_sidecar serve --port 8765
```

开发测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

可复现打包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1
```

打包要求 Python 3.12.13 和 `build-requirements.lock` 中的精确版本。build script 会运行 live/initialize/ready、WebSocket ping/pong、HTTP shutdown、exit code、Job cleanup、stdout 与 secret marker smoke。`dist/` 与复制到 `frontend/src-tauri/binaries/` 的 `.exe` 均为生成物，不提交到 Git。Sidecar 不负责 reconnect、自动重启或请求重放；异常退出由 Rust Supervisor 显式发布 `FAILED`。
