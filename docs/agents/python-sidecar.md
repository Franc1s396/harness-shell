# Python Sidecar Guide

## 何时必须读取

修改以下内容时必须读取本文档：

- `backend/src/harness_shell_sidecar/` 中的 Python Sidecar 代码；
- stdin/stdout transport、Router、Dispatcher 或 handler；
- Connection、SSH、Host Key、PTY、用户手动 SFTP、实验性 ReAct Agent 或历史 Artifact schema；
- Runtime SQLite、migration、加密记录、Audit 或 Trace；
- Python Sidecar 测试、启动或 PyInstaller 打包。

所有 Python 修改还必须读取 [Python Style Guide](python-style.md)。涉及 Protocol、凭据、事件或敏感数据时同时读取 [Protocol & Security Guide](protocol-security.md)。

## 范围与职责

Python Sidecar 是由 Tauri Rust Core 独占管理的本地子进程。它负责：

- 解析和编码私有 Protocol v1 stdin/stdout frame；
- 校验 initialize、request、cancel、shutdown 和 application payload；
- 管理 SSH connection、Host Key、人工 PTY 和隔离 channel；
- 管理 runtime SQLite migration、AES-GCM record、Audit HMAC chain 和 local Trace；
- 管理实验性 ReAct Agent、显式 Provider API 类型、绑定 Session 的 non-PTY command channel 和加密 conversation history；
- 通过受控 event listener 发布允许的连接与 PTY 事件。

Sidecar 不提供 TCP/HTTP 端口，不读取 DPAPI Vault，不直接与 WebView 通信，也不拥有自动重启或不确定请求重放策略。

## 当前源码真源

| 包 | 职责 | 主要真源 |
| --- | --- | --- |
| `connections` | Connection profile、Host Key record 与 repository | `models.py`、`repository.py`、`handlers.py` |
| `protocol` | Protocol v1 envelope、codec 和 terminal violation | `models.py`、`codec.py`、`errors.py` |
| `runtime` | stdio transport、Router、Dispatcher、Service、Windows Job attachment | `stdio.py`、`router.py`、`dispatcher.py`、`service.py`、`windows_job.py` |
| `ssh` | 认证、Host Key、session registry、AsyncSSH lifecycle | `auth.py`、`host_keys.py`、`sessions.py`、`runtime.py`、`handlers.py` |
| `storage` | SQLite、migration、AES-GCM、Audit、Trace；`artifact_metadata` 仅为历史 migration schema | `database.py`、`migrations/`、`encrypted_records.py`、`audit.py`、`traces.py` |
| `telemetry` | local-only OpenTelemetry exporter/provider 与完整 JSON stderr logging | `local_exporter.py`、`logging.py` |
| `terminal` | 人工 PTY model、manager 与 request handler | `models.py`、`manager.py`、`handlers.py` |
| `manual_sftp` | 用户手动 SFTP 的严格 payload、隔离 channel、mutation、transfer、encrypted operation record 与显式 recovery | `handlers.py`、`service.py`、`mutations.py`、`transfers.py`、`operation_store.py`、`recovery.py` |
| `agent` | 模型 API 配置、ReAct graph、五轮 context、绑定 Session 的 command tool、Provider gateway 与加密 conversation/run/message | `handlers.py`、`service.py`、`graph.py`、`context.py`、`executor.py`、`model_gateway.py`、`conversations.py` |

其他真源：

- 工程和依赖：[backend/pyproject.toml](../../backend/pyproject.toml)
- Sidecar 说明：[backend/README.md](../../backend/README.md)
- 测试：[backend/tests/](../../backend/tests/)
- 打包：[backend/scripts/build_sidecar.ps1](../../backend/scripts/build_sidecar.ps1)
- Protocol 文字规范：[docs/protocol/v1.md](../protocol/v1.md)

## 项目结构规范

- 外部 frame 先经 `protocol` 和 `runtime` 验证，再分派到领域 handler；领域模块不得自行解析原始 frame bytes。
- Handler 只负责 boundary validation、取消检查、领域调用和结构化错误映射；复杂状态与 I/O 进入 manager/runtime/repository。
- Pydantic model 定义跨边界数据；数据库 row 到领域 model 的转换集中在 repository/store 层。
- SSH connection、PTY process、SSH child channel 和 async task 各有唯一 registry/manager owner。
- `storage/migrations/` 只新增顺序编号 SQL migration，不原地重写已经发布的 migration。
- Runtime SQLite 当前为 schema v4。Connection profile 的 `version` 列范围仍为 `1..2^53-1`，创建为 `1`，repository 更新通过原子 `version = version + 1` 推进；达到上限返回 `CONNECTION_VERSION_EXHAUSTED` 且不修改记录，`updated_at` 不承担并发控制。
- 旧 `remote_io` 包已经删除；不得恢复宽泛 Agent exec/SFTP/Artifact compatibility layer。`manual_sftp` 只服务用户显式操作，经私有 Protocol method 由 Rust coordinator 调用；不得接入 Agent 工具、自动恢复或请求重放。实验性 Agent 命令只能经过 `agent.executor` 的严格单命令契约。
- `AgentService` 在 conversation lock 内重新验证完整 Provider config 快照、取消状态和 connected Session；lock entry 必须在最后一个 holder/waiter 离开后回收。`executor.py` 对所有未确定结果保留 child-channel 所有权，直到 `close()` 与 `wait_closed()` 成功；预取消不得跨过 `create_process` dispatch boundary。
- 新包或模块按单一职责建立；不得把 handler、数据库、transport 和安全策略堆入同一 convenience module。

## 代码规范

- 语言级 docstring、类字段、类型注解、异常、async 和 Review 规则以 [Python Style Guide](python-style.md) 为唯一真源。
- 所有外部输入使用 strict Pydantic model 或等价显式校验；未知字段、非 canonical 编码和越界值 fail closed。
- stdin/stdout 只承载 `Content-Length` frame；普通日志只能写 stderr，并完整保留调用方提供的内容。
- `telemetry/logging.py` 是普通日志唯一结构化入口：序列化完整 message、任意结构化字段与 exception traceback；HTTP 异常额外记录完整 response body，不执行字段 allowlist、脱敏、截断或哈希替代。
- Domain failure 使用稳定 `error_code` 和安全 message；不得把 raw exception、secret 或无界远程输出放入 response details。
- Cancel、timeout、EOF、shutdown 和异常退出必须显式传播到任务 owner，并确定性关闭 channel、process、database 和 exporter。
- 捕获 `BaseException` 仅用于确保 cleanup/zeroize 后重新抛出或保留首个 cleanup failure，不得吞掉取消或系统退出。
- 敏感 byte buffer 在生命周期结束时主动覆盖；调用方不得主动把秘密传给 Logger，统一日志层不会扫描或删除调用方与底层异常已经提供的内容。

## 长期约束

- Python project 支持 Python `>=3.12`；可复现 Sidecar 打包当前固定 Python `3.12.13` 和 `build-requirements.lock` 精确版本。
- Protocol header 上限、payload 上限、heartbeat 和 timeout 必须与 Rust 和协议规范一致。
- initialize 在 READY 前完成 migration、Audit chain 验证和 storage self-check；任何失败都不得发布 READY。
- SSH handler 只接受严格整数 `profile_version`，目标与 ProxyJump 的版本必须在任何网络 I/O 前匹配当前 repository 记录；旧 `profile_updated_at` 请求不兼容且必须 fail closed。
- Audit 是 append-only、tamper-evident；Trace 只保存 allowlist metadata；encrypted record 使用绑定身份的 AES-GCM。
- stdout 不允许混入日志。调用点自行决定写入 stderr 的内容，Logger 不做 key、credential、payload 或远程输出扫描。
- 结构化日志事件包括 logger 配置后、stdio service 构造前写入的 `sidecar_process_started`，以及 `sidecar_runtime_failed`、`request_handler_failed`、`model_request_failed`、`model_network_timeout`、`agent_node_started|completed|failed`、`agent_route_selected` 和 `agent_run_started|completed|cancelled|failed`。节点 wrapper 不改变七个节点、edge、patch、异常或取消语义；Run terminal 事件必须在 durable terminal state 后恰好写一次。
- Sidecar 异常退出后由 Rust Supervisor 决定显式状态；Python 不自启、不隐式恢复、不重放请求。
- Agent 的 API 类型只能是显式配置的 `CHAT_COMPLETIONS` 或 `RESPONSES`，禁止自动探测或切换；两种 API 类型都在 Provider gateway 内使用 `astream()` 并聚合成一个完整 `AIMessage`，不改变 Protocol 和 UI 的单结果非流式契约；网络 timeout 仅按固定策略最多重试 5 次，其他 Provider failure 立即显式失败。
- Agent 每次 turn 固定一个 live `ssh_session_id`，只开隔离 non-PTY exec channel；命令 30 秒超时、stdout/stderr 严格 UTF-8、危险模式与多 Tool Call fail closed，最多完成 128 次 ReAct Tool 循环。
- Agent 的原始 message/tool/output 以绑定 conversation/message 身份的 AES-GCM ciphertext 持久化。现有业务调用点不主动把 user message、API Key、command、stdout/stderr 或 model response 作为日志字段；Logger 不对异常文本或 HTTP response body 做二次过滤。
- `manual_sftp.recovery.execute` 必须接收 Rust 选择的 fresh `operation_id`，在任何 mutation I/O 前验证它不同于旧 recovery ID 且未被 encrypted operation store 使用，并把该 ID 原样用于新 mutation。
- `manual_sftp.delete.preflight` 必须接收 Rust 选择的 fresh `operation_id`，在保存 encrypted delete plan/operation record 前验证未被使用，并以该 ID 作为唯一 recursive-delete remote identity；不得生成替代 ID、猜测或重放 preflight。
- OpenSSH listing 返回的 `.`/`..` 由 listing owner 忽略且不计入 50,000 entry 上限；typed `SFTPPermissionDenied` 映射为确定的 `SFTP_PERMISSION_DENIED`。rename 在服务端支持 `statvfs` 时先比较 source/target parent `fsid` 并以 `SFTP_CROSS_DEVICE_MOVE_UNSUPPORTED` 在 dispatch 前拒绝跨文件系统移动，不实施 copy-delete fallback。
- `manual_sftp` 的 typed permission/not-found/unsupported/target-changed failure 必须在 handler 边界保留为可信 terminal error；Python 已持久化 `cleanup_required` 或 `outcome_unknown` 时，error payload 只能附带该精确 `operation_state`，Rust 必须保留 recovery journal。只有 transport/reply 不可信才允许 Rust自行归类为 unknown。
- SFTP channel open/close、metadata 与简单 mutation 单次请求使用 15 秒 deadline，chunk 请求使用 30 秒 deadline，完整 SHA-256 与 recursive work 使用 60 秒无进展 deadline；不设置任意总 wall-clock cap，也不自动重试 mutation。
- encrypted remote recovery record 冻结连接 `version`、安全 `display_name`、目标 Host Key 指纹以及可选 ProxyJump 的 connection/version/fingerprint；恢复只能绑定完全匹配且唯一的 live Session。相同 `connection_id` 的已编辑配置或已替换 Host Key 不能复用旧 recovery。
- SFTP v3 的 absent destination 必须使用标准 no-clobber rename（`flags=0`），因为 AsyncSSH 会把任意非零 v3 flags 映射为可覆盖目标的 OpenSSH POSIX rename；确认覆盖仍使用 atomic overwrite flags，且不得在目标并发出现后弱化重试。
- WebView 仅能经七个 main-window Tauri commands 使用实验性 Agent 配置、模型 API Key 和 turn；不能调用 raw Sidecar method。Agent 没有 SFTP/Artifact route，且不得把历史 `artifact_metadata` 表描述为仍存在 Artifact 运行时。
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
- Handler/Router：覆盖未初始化、非法 payload、cancel、异常映射和不发生 mutation 的失败路径。
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
