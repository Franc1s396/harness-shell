# Python Sidecar Guide

## 何时必须读取

修改以下内容时必须读取本文档：

- `backend/src/harness_shell_sidecar/` 中的 Python Sidecar 代码；
- FastAPI/Uvicorn、HTTP/WebSocket boundary、Dispatcher 或 handler；
- Connection、SSH、Host Key、PTY、用户手动 SFTP、实验性 ReAct Agent 或历史 Artifact schema；
- Runtime SQLite、migration、加密记录、Audit 或 Trace；
- Python Sidecar 测试、启动或 PyInstaller 打包。

所有 Python 修改还必须读取 [Python Style Guide](python-style.md)。涉及 Protocol、凭据、事件或敏感数据时同时读取 [Protocol & Security Guide](protocol-security.md)。

## 范围与职责

Python Sidecar 是由 Tauri Rust Core 独占管理的本地子进程。它负责：

- 提供 loopback-only typed HTTP API 与 single-owner Runtime WebSocket；
- 校验 initialize、request correlation、cancel、shutdown 和 application payload；
- 管理 SSH connection、Host Key、人工 PTY 和隔离 channel；
- 管理 runtime SQLite migration、AES-GCM record、Audit HMAC chain 和 local Trace；
- 管理实验性 ReAct Agent、显式 Provider API 类型、绑定 Session 的 non-PTY command channel 和加密 conversation history；
- 通过受控 event listener 发布允许的连接与 PTY 事件。

Sidecar 唯一 public 进程入口是 `serve --port <1..65535>`，固定在 `127.0.0.1` 启动 Uvicorn；不接受 host override，不读取 DPAPI Vault，不直接与 WebView 通信，也不拥有 reconnect、自动重启或不确定请求重放策略。Rust Core 负责选择动态端口、启动 packaged child、一次性注入 Runtime keys 并监督进程。

## 当前源码真源

| 包 | 职责 | 主要真源 |
| --- | --- | --- |
| `connections` | Connection profile、Host Key record 与 repository | `models.py`、`repository.py`、`handlers.py` |
| `runtime` | 共享 initialize model/phase、唯一 Runtime resources owner、transport-independent RequestContext/Dispatcher、Windows Job attachment | `models.py`、`resources.py`、`request_context.py`、`dispatcher.py`、`windows_job.py` |
| `web` | import-side-effect-free FastAPI factory、lifespan RuntimeOwner、1 MiB limits、Problem Details、50 个 typed HTTP operations、256 KiB SFTP binary boundary 与 single-owner typed Runtime WebSocket | `app.py`、`lifespan.py`、`dependencies.py`、`errors.py`、`limits.py`、`models.py`、`websocket.py`、`routes/` |
| `web.server` / `web.contracts` | 固定 loopback Uvicorn config、bind 后 structured readiness 与 deterministic OpenAPI/WebSocket export | `server.py`、`contracts.py`、`backend/scripts/export_http_contract.py` |
| `ssh` | 严格 request model、认证、Host Key、session registry、AsyncSSH lifecycle | `models.py`、`auth.py`、`host_keys.py`、`sessions.py`、`runtime.py`、`handlers.py` |
| `storage` | SQLite、migration、AES-GCM、Audit、Trace；`artifact_metadata` 仅为历史 migration schema | `database.py`、`migrations/`、`encrypted_records.py`、`audit.py`、`traces.py` |
| `telemetry` | local-only OpenTelemetry exporter/provider 与完整 JSON stderr logging | `local_exporter.py`、`logging.py` |
| `terminal` | 人工 PTY model、manager 与 request handler | `models.py`、`manager.py`、`handlers.py` |
| `manual_sftp` | 用户手动 SFTP 的严格 payload、隔离 channel、mutation、transfer、encrypted operation record 与显式 recovery | `handlers.py`、`service.py`、`mutations.py`、`transfers.py`、`operation_store.py`、`recovery.py` |
| `agent` | 模型 API 配置、ReAct graph、二十轮 context、绑定 Session 的 command tool、Provider gateway 与加密 conversation/run/message | `handlers.py`、`service.py`、`graph.py`、`context.py`、`executor.py`、`model_gateway.py`、`conversations.py` |

其他真源：

- 工程和依赖：[backend/pyproject.toml](../../backend/pyproject.toml)
- Sidecar 说明：[backend/README.md](../../backend/README.md)
- 测试：[backend/tests/](../../backend/tests/)
- 打包：[backend/scripts/build_sidecar.ps1](../../backend/scripts/build_sidecar.ps1)
- HTTP/WebSocket 契约：[docs/protocol/http/](../protocol/http/)

## 项目结构规范

- HTTP routes 将 strict typed input 转换为 `RequestContext + Mapping` 交给 application dispatcher；WebSocket PTY input 与 Manual SFTP binary routes 也必须复用该 request owner，领域 handler 不得解析原始 HTTP/WebSocket wire。
- `RuntimeResources.initialize(...)` 原子构造完整资源图；失败不得发布部分 owner。`shutdown()` 固定执行 dispatcher 收敛、Agent 引用清除、PTY、Manual SFTP、SSH、Trace、record/audit/key zeroize 与 DB close，并在全部阶段尝试完成后抛出首个清理错误。
- FastAPI `RuntimeOwner` 只保存一个 `RuntimeResources`；`LIVE_NOT_INITIALIZED → INITIALIZING → READY → DRAINING → CONVERGING → CLOSING → STOPPED|FAILED` 是 HTTP/资源 owner 的唯一生命周期模型。
- 当前 HTTP JSON request/response 上限为 encoded 1 MiB，dispatcher active capacity 为 16；`X-Request-ID` 缺失或非法返回 typed 400，validation/duplicate/capacity 分别映射 422/409/429。`/docs`、`/redoc` 与运行时 `/openapi.json` 禁用。
- 当前 HTTP route 共覆盖六个 Runtime endpoints、Connection/Host Key/SSH/PTY control/Agent 20 个 endpoints，以及 Manual SFTP 24 个 JSON/binary endpoints，总计 50 个 operations；`pty.write` 只经 `/v1/runtime/events` 的 strict `pty.input` 进入，HTTP 不提供 write route。
- `RuntimeWebSocketGateway` 独占一个 active Desktop connection 和两个 capacity=64 的 queue；65,536-byte text limit、显式 ping heartbeat、typed domain event converter 和 response causation 均在 gateway 内完成。SSH state、PTY output/closed 与 Manual SFTP progress 必须先按当前安全 domain model 重验证，raw dict 不得直接出站。
- `web.server` 固定 `proxy_headers=False`、access log/server/date headers disabled、`ws_max_size=65536`、`ws_max_queue=64` 和 transport ping disabled；bind failure 非零退出且不选择替代端口。成功 bind 才记录 `http_server_listening`。
- `backend/scripts/export_http_contract.py --check` 必须逐字节匹配 `docs/protocol/http/openapi-v1.json` 与 `runtime-websocket-v1.schema.json`；`--write` 只用于明确审阅后的契约重生成，不能作为让测试通过的隐式步骤。
- Handler 只负责 boundary validation、取消检查、领域调用和结构化错误映射；复杂状态与 I/O 进入 manager/runtime/repository。
- Pydantic model 定义跨边界数据；数据库 row 到领域 model 的转换集中在 repository/store 层。
- SSH connection、PTY process、SSH child channel 和 async task 各有唯一 registry/manager owner。
- `storage/migrations/` 只新增顺序编号 SQL migration，不原地重写已经发布的 migration。
- Runtime SQLite 当前为 schema v4。Connection profile 的 `version` 列范围仍为 `1..2^53-1`，创建为 `1`，repository 更新通过原子 `version = version + 1` 推进；达到上限返回 `CONNECTION_VERSION_EXHAUSTED` 且不修改记录，`updated_at` 不承担并发控制。
- 旧 `remote_io` 包已经删除；不得恢复宽泛 Agent exec/SFTP/Artifact compatibility layer。`manual_sftp` 只服务用户显式操作，经 sealed HTTP/binary request 由 Rust coordinator 调用；不得接入 Agent 工具、自动恢复或请求重放。实验性 Agent 命令只能经过 `agent.executor` 的严格单命令契约。
- Manual SFTP application chunk 接口使用原始 `bytes` 和 typed receipt；HTTP boundary 使用严格 `application/octet-stream`、canonical `Content-Length` 和 identity headers，领域 transfer state machine 不选择 wire encoding，也不存在 Base64 fallback adapter。
- `AgentService` 在 conversation lock 内重新验证完整 Provider config 快照、取消状态和 connected Session；lock entry 必须在最后一个 holder/waiter 离开后回收。`executor.py` 对所有未确定结果保留 child-channel 所有权，直到 `close()` 与 `wait_closed()` 成功；预取消不得跨过 `create_process` dispatch boundary。
- 新包或模块按单一职责建立；不得把 handler、数据库、transport 和安全策略堆入同一 convenience module。

## 代码规范

- 语言级 docstring、类字段、类型注解、异常、async 和 Review 规则以 [Python Style Guide](python-style.md) 为唯一真源。
- 所有外部输入使用 strict Pydantic model 或等价显式校验；未知字段、非 canonical 编码和越界值 fail closed。
- `serve` 模式的 stdout 必须保持空；普通日志只写 stderr，并完整保留调用方提供的内容。HTTP/WebSocket 边界不得依赖 stdout ready marker、framing 或控制消息。
- `telemetry/logging.py` 是普通日志唯一结构化入口：序列化完整 message、任意结构化字段与 exception traceback；HTTP 异常额外记录完整 response body，不执行字段 allowlist、脱敏、截断或哈希替代。
- Domain failure 使用稳定 `error_code` 和安全 message；不得把 raw exception、secret 或无界远程输出放入 response details。
- Cancel、timeout、EOF、shutdown 和异常退出必须显式传播到任务 owner，并确定性关闭 channel、process、database 和 exporter。
- 捕获 `BaseException` 仅用于确保 cleanup/zeroize 后重新抛出或保留首个 cleanup failure，不得吞掉取消或系统退出。
- 敏感 byte buffer 在生命周期结束时主动覆盖；调用方不得主动把秘密传给 Logger，统一日志层不会扫描或删除调用方与底层异常已经提供的内容。

## 长期约束

- Python project 支持 Python `>=3.12`；可复现 Sidecar 打包当前固定 Python `3.12.13` 和 `build-requirements.lock` 精确版本。
- HTTP/WS payload 上限、heartbeat 和 timeout 必须与 Rust 和公共契约一致。
- initialize 在 READY 前完成 migration、Audit chain 验证和 storage self-check；任何失败都不得发布 READY。
- SSH handler 只接受严格整数 `profile_version`，目标与 ProxyJump 的版本必须在任何网络 I/O 前匹配当前 repository 记录；旧 `profile_updated_at` 请求不兼容且必须 fail closed。
- Audit 是 append-only、tamper-evident；Trace 只保存 allowlist metadata；encrypted record 使用绑定身份的 AES-GCM。
- stdout 不允许混入日志。调用点自行决定写入 stderr 的内容，Logger 不做 key、credential、payload 或远程输出扫描。
- 结构化日志事件包括 Uvicorn 启动前写入的 `sidecar_process_started` 和 bind 后的 `http_server_listening`，以及 `sidecar_runtime_failed`、`request_handler_failed`、`model_request_failed`、`model_network_timeout`、`agent_node_started|completed|failed`、`agent_route_selected` 和 `agent_run_started|completed|cancelled|failed`。节点 wrapper 不改变七个节点、edge、patch、异常或取消语义；Run terminal 事件必须在 durable terminal state 后恰好写一次。
- Sidecar 异常退出后由 Rust Supervisor 决定显式状态；Python 不自启、不隐式恢复、不重放请求。
- Agent 的 API 类型只能是显式配置的 `CHAT_COMPLETIONS` 或 `RESPONSES`，禁止自动探测或切换；两种 API 类型都在 Provider gateway 内使用 `astream()` 并聚合成一个完整 `AIMessage`，不改变 HTTP 和 UI 的单结果非流式契约；网络 timeout 仅按固定策略最多重试 5 次，其他 Provider failure 立即显式失败。
- Agent 每次 turn 固定一个 live `ssh_session_id`，只开隔离 non-PTY exec channel；命令 30 秒超时、stdout/stderr 严格 UTF-8、危险模式与多 Tool Call fail closed，最多完成 128 次 ReAct Tool 循环。
- Agent 的原始 message/tool/output 以绑定 conversation/message 身份的 AES-GCM ciphertext 持久化。现有业务调用点不主动把 user message、API Key、command、stdout/stderr 或 model response 作为日志字段；Logger 不对异常文本或 HTTP response body 做二次过滤。
- `manual_sftp.recovery.execute` 必须接收 Rust 选择的 fresh `operation_id`，在任何 mutation I/O 前验证它不同于旧 recovery ID 且未被 encrypted operation store 使用，并把该 ID 原样用于新 mutation。
- `manual_sftp.delete.preflight` 必须接收 Rust 选择的 fresh `operation_id`，在保存 encrypted delete plan/operation record 前验证未被使用，并以该 ID 作为唯一 recursive-delete remote identity；不得生成替代 ID、猜测或重放 preflight。
- OpenSSH listing 返回的 `.`/`..` 由 listing owner 忽略且不计入 50,000 entry 上限；typed `SFTPPermissionDenied` 映射为确定的 `SFTP_PERMISSION_DENIED`。rename 在服务端支持 `statvfs` 时先比较 source/target parent `fsid` 并以 `SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED` 在 dispatch 前拒绝跨文件系统移动，不实施 copy-delete fallback。
- `manual_sftp` 的 typed permission/not-found/unsupported/target-changed failure 必须在 handler 边界保留为可信 terminal error；Python 已持久化 `cleanup_required` 或 `outcome_unknown` 时，error payload 只能附带该精确 `operation_state`，Rust 必须保留 recovery journal。只有 transport/reply 不可信才允许 Rust自行归类为 unknown。
- SFTP channel open/close、metadata 与简单 mutation 单次请求使用 15 秒 deadline，chunk 请求使用 30 秒 deadline，完整 SHA-256 与 recursive work 使用 60 秒无进展 deadline；不设置任意总 wall-clock cap，也不自动重试 mutation。
- encrypted remote recovery record 冻结连接 `version`、安全 `display_name`、目标 Host Key 指纹以及可选 ProxyJump 的 connection/version/fingerprint；恢复只能绑定完全匹配且唯一的 live Session。相同 `connection_id` 的已编辑配置或已替换 Host Key 不能复用旧 recovery。
- SFTP v3 的 absent destination 必须使用标准 no-clobber rename（`flags=0`），因为 AsyncSSH 会把任意非零 v3 flags 映射为可覆盖目标的 OpenSSH POSIX rename；确认覆盖仍使用 atomic overwrite flags，且不得在目标并发出现后弱化重试。
- WebView 仅能经七个 main-window Tauri commands 使用实验性 Agent 配置、模型 API Key 和 turn；不能访问 Python base URL 或 raw HTTP/WebSocket。Agent 没有 SFTP/Artifact route，且不得把历史 `artifact_metadata` 表描述为仍存在 Artifact 运行时。
- `backend/build/`、`backend/dist/`、`.venv/`、cache 和复制到 Tauri binaries 的 `.exe` 均为生成物。

## 项目命令

以下命令从 `backend/` 运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m harness_shell_sidecar
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1
```

- 普通开发和测试遵循 `pyproject.toml` 的 Python `>=3.12`。
- `build_sidecar.ps1` 严格要求 Python `3.12.13`、锁定依赖和预期 PyInstaller 输出；不满足时应直接失败。

## 验证要求

- Model/validator：运行相邻 model test，覆盖 unknown field、canonical encoding、上下界和结构化错误。
- Handler/routes：覆盖未初始化、非法 payload、cancel、异常映射和不发生 mutation 的失败路径。
- async runtime：覆盖正常结束、timeout、cancel、EOF、handler failure 和所有资源 cleanup。
- SSH/PTY/remote I/O：除 unit test 外，按变更范围运行 `backend/tests/ssh_integration/` 或完整 M2 门禁。
- Storage/migration：覆盖新旧数据库启动、migration 原子性、密文不泄漏、Audit tamper 和 Trace allowlist。
- 打包路径：执行 build script、smoke test 和 packaged Rust contract；未实际打包时不得报告打包通过。

## 何时需要更新本文档

以下变化必须同步更新本文档：

- Python package、module、manager 或 repository 的职责/路径改变；
- 新增 handler、SSH/PTY/remote I/O 能力或 WebView 路由；
- Python/runtime/build 版本、依赖边界、启动或打包命令改变；
- storage migration、Audit、Trace、加密或 secret 生命周期改变；
- shutdown、cancel、timeout、resource owner 或错误传播策略改变。

仅修改函数内部实现且不改变上述长期事实时无需更新，但必须执行 AGENTS 影响检查。
