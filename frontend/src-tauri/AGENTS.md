# Tauri Rust Core Local Rules

## 必读路由

修改本目录前，先读取仓库根 `AGENTS.md`，并按任务读取：

- [Architecture Guide](../../docs/agents/architecture.md)
- [Rust Core Guide](../../docs/agents/rust-core.md)
- [Protocol & Security Guide](../../docs/agents/protocol-security.md)
- [Testing Guide](../../docs/agents/testing.md)

## 本目录即时规则

- 本 crate 只是最小 Tauri UI shell；Launcher 独占 Sidecar/UI child、Windows Job、动态端口和退出顺序，Python 独占凭据与业务 runtime。不得把这些 owner 移回本目录。
- custom command 仅允许 `get_backend_bootstrap`；修改时同步检查实现、re-export、handler、permission、capability、Frontend wrapper 和契约测试。
- capability/permission 保持最小授权；只允许 main window 获得 bootstrap 与固定 close/destroy 权限，不得重新引入独立 approval window。
- raw secret、private key、passphrase、HTTP/WebSocket body、Backend stderr 和 SFTP bytes 不得进入 Tauri event、日志、`Debug` 或普通错误详情。
- 本 crate 不实现业务 HTTP/WebSocket client、日志目录 API、通用 shell/filesystem capability、child process 或 Job Object。
- production 缺失/非法 `--backend-url` 必须 native error 后退出；不得扫描端口、fallback 到默认地址或伪造 bootstrap。
- 至少运行最小相关 Rust test；跨层或打包改动按 Testing Guide 扩大验证范围。
- 任务结束前检查上述领域文档是否因长期事实变化需要同步更新，并报告结果。
