# Rust Core Guide

## 当前 Rust 边界

Rust 分为两个独立 crate：

- `launcher/`：production Desktop lifecycle owner。
- `frontend/src-tauri/`：最小 UI shell。

不得在 Tauri crate 中重新建立 Backend client、business proxy、凭据仓库、Runtime supervisor、WebSocket projection 或 Manual SFTP local-file actor。

## Launcher

Launcher 从自己的绝对路径解析同目录 `harness-shell-ui.exe` 与 `harness-shell-sidecar.exe`，并使用 `%LOCALAPPDATA%\com.harnessshell.app`。它必须：

1. 创建 Windows Job 与不可继承的 parent pipe ends。
2. suspended spawn Backend，仅继承 control-read/ready-write handles，然后加入 Job 并 resume。
3. 读取长度有界、strict JSON 的 ready frame，拒绝未知字段、重复字段、port 0、超时和提前退出。
4. 用 ready frame 的 port 启动 UI；禁止端口扫描。
5. UI 退出后发一个 graceful byte并有界等待；失败或超时终止 Job。
6. 通过唯一 inherited stderr pipe 持续排空 Backend 日志，写入 `logs\harness-shell-backend.log`，单文件上限 10 MiB 并保留 4 个归档；日志写入线程必须在 child/Job 收敛后 join。
7. 不 reconnect、不 respawn、不把 child stderr 或 secret 放入用户错误框、Tauri 或 WebView。

复杂 handle、Job、spawn 和 failure-path 代码必须注释资源所有权与清理顺序。

## Tauri UI shell

`frontend/src-tauri/src/commands/` 只允许：

- `get_backend_bootstrap`：读取唯一 `--backend-url`，只接受 `http://127.0.0.1:<nonzero>`。

custom permissions 只能是 `bootstrap.toml`。main capability 只有 bootstrap 和必要的固定 window close/destroy 权限；不存在独立 approval capability。Release UI 未经 Launcher bootstrap 必须显示稳定 native startup error 后退出。

Tauri 自身的 `harness-shell.log` 与 Launcher 写入的 Backend 日志相互独立，均使用设备本地时区；两个进程不得并发写入同一日志文件。

## 打包

- target 固定 `x86_64-pc-windows-msvc`。
- Tauri `mainBinaryName` 固定 `harness-shell-ui`，bundle target 只为 NSIS。
- external binaries 包含 target-triple Backend 与 Launcher companion。
- custom NSIS template 必须与 lockfile 中 Tauri CLI 版本一致；Start Menu、Desktop shortcut、finish action 和 silent `/R` 都只启动 Launcher。
- `scripts/build-desktop.ps1` 按 Backend → Launcher → Frontend → NSIS 顺序 fail fast。

验证：

```powershell
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets --offline
cargo test --manifest-path launcher\Cargo.toml --all-targets --offline
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-installer-entry.ps1
```

Rust tests和静态 installer 检查不等于安装到一次性 Windows 用户后的 Desktop 人工验收。
