# Harness Shell Sidecar

M1 Python Sidecar 是由 Tauri Rust Core 独占管理的本地子进程，不提供 TCP/HTTP 监听端口。它通过 stdin/stdout 接收 protocol v1 `Content-Length` 帧，完成初始化、heartbeat 和正常 shutdown。

初始化要求绝对路径的 runtime SQLite 数据库、两个 canonical base64 32-byte 密钥、5 秒 heartbeat 和 15 秒 timeout。启动会执行 schema migration、Audit HMAC chain 验证和本地存储 self-check；Audit 篡改会返回 `AUDIT_CHAIN_INVALID` 并在 `READY` 前退出。

运行时数据库只保存 AES-GCM 密文、allowlist Trace 属性和 append-only Audit。stdout 仅用于协议；日志写 stderr，且不得包含密钥、payload 或持久化明文。

开发测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

可复现打包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1
```

打包要求 Python 3.12.13 和 `build-requirements.lock` 中的精确版本。`dist/` 与复制到 `frontend/src-tauri/binaries/` 的 `.exe` 均为生成物，不提交到 Git。Sidecar 不负责自动重启；异常退出后的 `PAUSED` 决策由 Rust Supervisor 管理。
