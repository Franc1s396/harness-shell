# Python Sidecar Local Rules

## 必读路由

修改本目录前，先读取仓库根 `AGENTS.md`，并按任务读取：

- [Architecture Guide](../../../docs/agents/architecture.md)
- [Python Sidecar Guide](../../../docs/agents/python-sidecar.md)
- [Python Style Guide](../../../docs/agents/python-style.md)
- [Protocol & Security Guide](../../../docs/agents/protocol-security.md)
- [Testing Guide](../../../docs/agents/testing.md)

## 本目录即时规则

- stdout 必须保持为空；所有结构化日志写 stderr，Logger 完整保留调用方传入的 message、字段与异常文本。
- `telemetry/logging.py` 独占结构化 stderr 格式；`log_event` / `log_exception_event` 不做字段 allowlist、脱敏、截断或正文替换，HTTP 异常同时记录完整 response body。
- application dispatcher 只接收 `RequestContext + Mapping`；HTTP route/WebSocket gateway 必须在边界完成严格验证后才能进入领域或 I/O 层，未知字段、非法编码和关联不一致 fail closed。
- `runtime/resources.py` 独占初始化后 database、repositories、SSH/PTY/Manual SFTP/Agent、Audit、Trace、dispatcher 与 key buffers；ASGI lifespan 只能持有一个 `RuntimeResources`，不得复制初始化或 shutdown owner。
- `web/` 是 public `serve --port` 使用的 loopback HTTP/WebSocket application boundary：app import 不得打开 DB、生成 key 或启动 task；lifespan 独占 `RuntimeOwner` 和 single-owner `RuntimeWebSocketGateway`，routes 必须要求 `X-Request-ID`、执行 strict validation、复用 application dispatcher 并返回 typed Problem/response。Manual SFTP binary routes 与 WebSocket PTY input 必须经 dispatcher typed execution boundary 调用 raw-byte application，不能绕过 duplicate/capacity/cancel owner。
- HTTP route 不得记录 body/header/credential/runtime key/user message/command/model response/stdout/stderr/SFTP bytes；unexpected exception 日志只允许 stable event/error code 与 exception type，不得把 exception text 当作安全内容。
- 当前 HTTP routes 不包括 `pty.write` 或 generic RPC；PTY bytes 只经 Runtime WebSocket `pty.input`，generic RPC 禁止实现。Manual SFTP 已具有完整 24 个 typed JSON/binary endpoints，禁止额外 Base64 HTTP、path-embedded remote path 或兼容 route。
- WebSocket inbound/outbound queue 都固定 capacity=64，不得 drop/merge；text encoded 上限 65,536 bytes。只有 strict `runtime.ping` 刷新 heartbeat，其他业务 message 不得作为隐式 liveness；第二连接不得抢占 active owner。
- `web/server.py` 独占 Uvicorn 配置和 bind 后 readiness 日志；只允许 `127.0.0.1` 和显式 port，禁止 host override、proxy headers、access log、server/date headers、transport ping 或自动换端口。`__main__.py` 不得增加第二种 public transport path。
- `web/contracts.py` 与 `backend/scripts/export_http_contract.py` 从实际 routes/models 生成 deterministic artifacts；检查失败必须暴露 schema drift，不能在普通测试路径自动写回。
- 调用方不得主动把 password、private key、passphrase、runtime key 或 raw secret frame 传给 Logger；统一日志层不会替调用方扫描或删除这些内容。
- SSH connection、PTY/channel、async task、database、exporter 和 secret buffer 必须有明确 owner、取消语义与确定性 cleanup。
- HTTP/WebSocket route、event、error code 或 payload shape 改动必须同步 Rust 侧、fixture、协议文档和契约测试。
- Migration 只新增顺序编号文件；Audit、Trace allowlist 和 encrypted record 约束不得被绕过。
- 所有 Python 变更执行 Python Style Guide 的 Code Review 检查清单；新建及实质修改的类、字段、函数和方法补齐准确注释。
- 至少运行最小相关 Pytest；SSH、PTY、存储、打包或跨层变更按 Testing Guide 扩大验证。
- 任务结束前检查上述领域文档是否因长期事实变化需要同步更新，并报告结果。
