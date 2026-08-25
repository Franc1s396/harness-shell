# M1 桌面基础设施验收

## 自动化证据

在同一 checkout、Windows 主机和同一终端中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
```

保存完整命令输出，并确认六个阶段均退出 `0`。证据必须包含测试数量、smoke test 成功后打印的 Sidecar target-triple 产物路径、双页 Web 构建产物和 `tauri info` 的 Rust/MSVC/WebView2 信息。输出不得包含 Vault 明文、运行时数据密钥或 Audit HMAC 密钥。

## 手工桌面安全检查

```powershell
npm.cmd run tauri:dev --prefix frontend
```

在不提交的本地测试记录中写下时间和界面显示的 correlation ID，然后逐项确认：

1. 主窗口从 `STARTING`/`HANDSHAKING` 到达 `READY`，并显示非空 correlation ID。
2. “Open approval window” 打开 label 固定的独立窗口；窗口显示 `pending=false` 和 “No approval request is pending.”。
3. 在主窗口 DevTools 执行 `window.__TAURI_INTERNALS__.invoke("submit_approval_decision")`，调用被 ACL 拒绝；不得收到 `NO_PENDING_APPROVAL`，因为该命令不属于主窗口。
4. 在任务管理器结束 `harness-shell-sidecar.exe`。主窗口转为 `PAUSED`、`recoverable=true`、错误码 `SIDECAR_EXITED`，correlation ID 不变。
5. 等待至少 16 秒并确认没有新的 Sidecar 进程自动出现。
6. 正常关闭桌面应用后确认没有残留 Sidecar 进程。

## 证据边界

M1 只验收本地桌面 Core、私有 stdio 协议、加密本地存储、审计/Trace、Capability 边界和 Sidecar 生命周期。它不证明 SSH/SFTP、AI Provider、远程主机、部署、迁移或真实生产环境可用。若自动化阶段通过但手工桌面检查未完成，不得宣称 M1 桌面验收完成。
