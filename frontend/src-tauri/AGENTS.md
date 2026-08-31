# Tauri Rust Core Local Rules

## 必读路由

修改本目录前，先读取仓库根 `AGENTS.md`，并按任务读取：

- [Architecture Guide](../../docs/agents/architecture.md)
- [Rust Core Guide](../../docs/agents/rust-core.md)
- [Protocol & Security Guide](../../docs/agents/protocol-security.md)
- [Testing Guide](../../docs/agents/testing.md)

## 本目录即时规则

- Rust Core 是凭据、DPAPI Vault、Sidecar 进程和 WebView 暴露面的安全边界；不得绕过受控 Tauri command 暴露特权能力。
- 新增或修改 command 时，同步检查实现、re-export、handler 注册、permission、capability、Frontend typed wrapper 和契约测试。
- capability/permission 保持按窗口最小授权；不得让 approval window 获得 main window 的 SSH 或 terminal 权限。
- raw secret、private key、passphrase、runtime key、raw frame 和 Sidecar stderr 不得进入 event、日志、`Debug` 或普通错误详情。
- `src/logging.rs` 独占 Tauri terminal/LogDir/rotation policy；`src/commands/diagnostics.rs` 独占固定日志目录查询与 Explorer 打开命令。不得新增 WebView 路径输入、日志内容 API 或通用 shell/filesystem capability。
- Protocol model、codec、limit 或 event 改动必须同步 Python 侧、fixture、协议文档和 Rust/Python 契约测试。
- Broker/Supervisor 失败必须显式可见；未知 response/event、heartbeat timeout 或 Sidecar crash 均不得静默降级、自动重放或伪造 READY。
- async task、channel、child process、Job Object、temporary extraction 和锁必须有明确 owner 与关闭路径。
- 至少运行最小相关 Rust test；跨层或打包改动按 Testing Guide 扩大验证范围。
- 任务结束前检查上述领域文档是否因长期事实变化需要同步更新，并报告结果。
