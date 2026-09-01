# Python Sidecar Local Rules

## 必读路由

修改本目录前，先读取仓库根 `AGENTS.md`，并按任务读取：

- [Architecture Guide](../../../docs/agents/architecture.md)
- [Python Sidecar Guide](../../../docs/agents/python-sidecar.md)
- [Python Style Guide](../../../docs/agents/python-style.md)
- [Protocol & Security Guide](../../../docs/agents/protocol-security.md)
- [Testing Guide](../../../docs/agents/testing.md)

## 本目录即时规则

- stdin/stdout 只允许 Protocol v1 frame；所有普通日志写 stderr，Logger 完整保留调用方传入的 message、字段与异常文本。
- `telemetry/logging.py` 独占结构化 stderr 格式；`log_event` / `log_exception_event` 不做字段 allowlist、脱敏、截断或正文替换，HTTP 异常同时记录完整 response body。
- request payload 必须在 Router/handler 边界完成严格验证后才能进入领域或 I/O 层；未知字段和非法编码 fail closed。
- 调用方不得主动把 password、private key、passphrase、runtime key 或 raw secret frame 传给 Logger；统一日志层不会替调用方扫描或删除这些内容。
- SSH connection、PTY/channel、async task、database、exporter 和 secret buffer 必须有明确 owner、取消语义与确定性 cleanup。
- Protocol、event、error code 或 payload shape 改动必须同步 Rust 侧、fixture、协议文档和契约测试。
- Migration 只新增顺序编号文件；Audit、Trace allowlist 和 encrypted record 约束不得被绕过。
- 所有 Python 变更执行 Python Style Guide 的 Code Review 检查清单；新建及实质修改的类、字段、函数和方法补齐准确注释。
- 至少运行最小相关 Pytest；SSH、PTY、存储、打包或跨层变更按 Testing Guide 扩大验证。
- 任务结束前检查上述领域文档是否因长期事实变化需要同步更新，并报告结果。
