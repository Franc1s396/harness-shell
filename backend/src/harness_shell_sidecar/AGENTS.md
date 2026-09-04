# Python Sidecar Local Rules

## 必读路由

修改本目录前，先读取仓库根 `AGENTS.md`，并按任务读取：

- [Architecture Guide](../../../docs/agents/architecture.md)
- [Python Sidecar Guide](../../../docs/agents/python-sidecar.md)
- [Python Style Guide](../../../docs/agents/python-style.md)
- [Protocol & Security Guide](../../../docs/agents/protocol-security.md)
- [Testing Guide](../../../docs/agents/testing.md)

## 本目录即时规则

- stdout 必须保持为空；所有日志写 stderr，格式固定为 `timestamp | level | reqId | thread | logger | message`，调用点只提交经过安全审查的稳定字段。源码 `serve` 模式为 PyCharm 等开发控制台输出 ANSI 分级颜色；Launcher 使用的 `desktop` 模式保持无 ANSI 的纯文本。
- `telemetry/logging.py` 独占 console formatter 与 request ID context。业务代码直接调用标准 `Logger.debug/info/warning/error` 并使用 `%s` 参数化，不再增加日志 helper wrapper；Logger 不替调用方做秘密扫描，因此 route、model 和 I/O 调用点不得传入 request/response body、credential 或远程输出。
- application dispatcher 只接收 `RequestContext + Mapping`；HTTP route/WebSocket gateway 必须在边界完成严格验证后才能进入领域或 I/O 层，未知字段、非法编码和关联不一致 fail closed。
- `runtime/resources.py` 独占基于 `RuntimeSettings` 初始化后的 plaintext database、repositories、SSH/PTY/Manual SFTP/Agent 与 dispatcher；ASGI lifespan 只能持有一个 `RuntimeResources`，不得复制初始化或 shutdown owner。
- `web/` 是 `serve` 与 `desktop` 共用的 loopback HTTP/WebSocket application boundary：app import 不得打开 DB 或启动 task；lifespan 独占 `RuntimeOwner` 和 single-owner `RuntimeWebSocketGateway`。routes 必须要求 `X-Request-ID`、strict validation、复用 dispatcher，并返回 typed Problem/response；Agent turn success 是独立 POST SSE response，不进入 Runtime WebSocket。
- HTTP access middleware 除 `GET /v1/runtime/state` 外，每个请求只记录一条完成日志，包含 method、route template、返回 status、duration 与 `X-Request-ID`；该 Runtime 轮询接口不打印 access log。不得记录 raw path/query/body/header/credential/user message/command/model response/stdout/stderr/SFTP bytes。`2xx/3xx` 使用 INFO，`4xx` 使用 WARNING，`5xx` 使用 ERROR；unexpected exception 日志只允许 stable event/error code 与 exception type。
- 当前 47 个 HTTP operations 不包括独立 credential mutation、approval、request cancel、SSH session list、单记录 Connection/Provider 查询、SFTP realpath、`pty.write` 或 generic RPC；Connection 与 Provider handler 在同一 SQLite 事务内维护业务记录及其凭据。PTY bytes 只经 Runtime WebSocket `pty.input`，generic RPC 禁止实现。Manual SFTP 具有 24 个 typed JSON/binary endpoints，禁止额外 Base64 HTTP、path-embedded remote path 或兼容 route。
- `POST /v1/agent/turns` 要求 `Accept: text/event-stream`；durable RUNNING 与 started 安全入队前失败为 Problem Details，此后只发送 strict Agent SSE terminal failure。per-turn queue capacity=64、frame=65,536 bytes、body=4,194,304 bytes、terminal reserve=65,536 bytes，producer 不得 drop/merge/truncate。
- WebSocket inbound/outbound queue 都固定 capacity=64，不得 drop/merge；text encoded 上限 65,536 bytes。只有 strict `runtime.ping` 刷新 heartbeat，其他业务 message 不得作为隐式 liveness；第二连接不得抢占 active owner。
- `web/server.py` 独占 Uvicorn 配置；只允许 `127.0.0.1`。`serve` 要求显式 nonzero port 与绝对 data dir；`desktop` 要求 port 0 和 Launcher inherited control/ready handles。Uvicorn native access log 必须关闭，原生启动细节固定在 WARNING threshold；禁止 host override、proxy headers、自动 fallback 或第二种 transport。
- `web/contracts.py` 与 `backend/scripts/export_http_contract.py` 从实际 routes/models 生成 deterministic artifacts；检查失败必须暴露 schema drift，不能在普通测试路径自动写回。
- 调用方不得主动把 password、private key、passphrase 或 raw secret frame 传给 Logger；统一日志层不会替调用方扫描或删除这些内容。
- SSH connection、PTY/channel、async task、database、exporter 和 secret buffer 必须有明确 owner、取消语义与确定性 cleanup。
- HTTP/WebSocket route、event、error code 或 payload shape 改动必须同步 React client、fixture、协议文档和契约测试。
- Runtime SQLite 只接受全新 schema v6；旧库在任何写入前 fail closed。业务、credential 与 recovery record 是 plaintext；没有 SQLite Audit/Trace/Artifact 表。
- 所有 Python 变更执行 Python Style Guide 的 Code Review 检查清单；新建及实质修改的类、字段、函数和方法补齐准确注释。
- 至少运行最小相关 Pytest；SSH、PTY、存储、打包或跨层变更按 Testing Guide 扩大验证。
- 任务结束前检查上述领域文档是否因长期事实变化需要同步更新，并报告结果。
