# Python Sidecar Local Rules

## 必读路由

修改本目录前，先读取仓库根 `AGENTS.md`，并按任务读取：

- [Architecture Guide](../../../docs/agents/architecture.md)
- [Python Sidecar Guide](../../../docs/agents/python-sidecar.md)
- [Python Style Guide](../../../docs/agents/python-style.md)
- [Protocol & Security Guide](../../../docs/agents/protocol-security.md)
- [Testing Guide](../../../docs/agents/testing.md)

## 本目录即时规则

- stdin/stdout 只允许 Protocol v1 frame；所有普通日志写 stderr，并保持有界、脱敏。
- `telemetry/logging.py` 独占结构化 stderr 格式、字段 allowlist 与安全异常提取；其他模块只通过 `log_event` / `log_exception_event` 写已批准事件，不直接序列化异常 message 或业务 payload。
- request payload 必须在 Router/handler 边界完成严格验证后才能进入领域或 I/O 层；未知字段和非法编码 fail closed。
- password、private key、passphrase、runtime key、raw secret frame 和无界远程输出不得进入日志、Trace、异常详情或持久化明文。
- SSH connection、PTY/channel、async task、database、exporter 和 secret buffer 必须有明确 owner、取消语义与确定性 cleanup。
- Protocol、event、error code 或 payload shape 改动必须同步 Rust 侧、fixture、协议文档和契约测试。
- Migration 只新增顺序编号文件；Audit、Trace allowlist 和 encrypted record 约束不得被绕过。
- 所有 Python 变更执行 Python Style Guide 的 Code Review 检查清单；新建及实质修改的类、字段、函数和方法补齐准确注释。
- 至少运行最小相关 Pytest；SSH、PTY、存储、打包或跨层变更按 Testing Guide 扩大验证。
- 任务结束前检查上述领域文档是否因长期事实变化需要同步更新，并报告结果。
