# Protocol & Security Guide

## 何时必须读取

出现以下任一情况时必须读取本文档：

- 修改 Rust/Python Protocol model、codec、frame、sequence、request correlation 或 event；
- 新增 Tauri command、Sidecar method、handler 或跨进程数据字段；
- 修改凭据、Host Key、DPAPI Vault、runtime key、日志、Trace、Audit 或持久化；
- 修改 WebView capability/permission、事件白名单或 approval 边界；
- 处理协议不兼容、payload 越界、Sidecar crash、heartbeat timeout 或 decoder failure。

跨层设计同时读取 [Architecture Guide](architecture.md)，验证范围同时读取 [Testing Guide](testing.md)。

## 范围与职责

Protocol v1 是 Tauri Rust Core 与 Python Sidecar 之间唯一受支持的私有 transport 契约。安全边界包括：

- Rust Core 独占 child-process pipe、DPAPI Vault、runtime key 注入和 WebView 权限；
- Python Sidecar 严格验证 envelope 和 application payload，管理 runtime secret 的最短生命周期；
- 双方只记录允许的 envelope metadata，不观察或持久化 secret payload；
- WebView 只能接收固定 command 返回和白名单事件投影。

协议错误、安全不变量失败或不可信持久化必须 fail closed，不通过兼容猜测、降级 transport 或静默跳过继续运行。

## 当前源码真源

- 文字规范：[docs/protocol/v1.md](../protocol/v1.md)
- Golden fixture：[docs/protocol/fixtures/valid-heartbeat-v1.json](../protocol/fixtures/valid-heartbeat-v1.json)
- Rust envelope/codec：[frontend/src-tauri/src/protocol/](../../frontend/src-tauri/src/protocol/)
- Python envelope/codec：[backend/src/harness_shell_sidecar/protocol/](../../backend/src/harness_shell_sidecar/protocol/)
- Rust Broker/event allowlist：[frontend/src-tauri/src/sidecar/broker.rs](../../frontend/src-tauri/src/sidecar/broker.rs)
- Python Router/stdio：[backend/src/harness_shell_sidecar/runtime/router.py](../../backend/src/harness_shell_sidecar/runtime/router.py)、[backend/src/harness_shell_sidecar/runtime/stdio.py](../../backend/src/harness_shell_sidecar/runtime/stdio.py)
- Rust Vault：[frontend/src-tauri/src/vault/](../../frontend/src-tauri/src/vault/)
- Python secret handling：[backend/src/harness_shell_sidecar/ssh/auth.py](../../backend/src/harness_shell_sidecar/ssh/auth.py)、[backend/src/harness_shell_sidecar/storage/](../../backend/src/harness_shell_sidecar/storage/)
- Python 完整日志与 Rust stderr owner：[backend/src/harness_shell_sidecar/telemetry/logging.py](../../backend/src/harness_shell_sidecar/telemetry/logging.py)、[frontend/src-tauri/src/sidecar/process.rs](../../frontend/src-tauri/src/sidecar/process.rs)
- Agent Protocol fixture：[docs/protocol/fixtures/agent/](../protocol/fixtures/agent/)
- Contract tests：[frontend/src-tauri/tests/protocol_contract.rs](../../frontend/src-tauri/tests/protocol_contract.rs)、[frontend/src-tauri/tests/broker_contract.rs](../../frontend/src-tauri/tests/broker_contract.rs)、[frontend/src-tauri/tests/manual_sftp_protocol_contract.rs](../../frontend/src-tauri/tests/manual_sftp_protocol_contract.rs)、[frontend/src-tauri/tests/agent_protocol_contract.rs](../../frontend/src-tauri/tests/agent_protocol_contract.rs)、[backend/tests/protocol/](../../backend/tests/protocol/)、[backend/tests/manual_sftp/](../../backend/tests/manual_sftp/)、[backend/tests/agent/](../../backend/tests/agent/)

## 项目结构规范

- Rust/Python 各自在 `protocol/` 保存 transport envelope 和 codec，不把领域 payload 解析塞入 framing 层。
- Application payload 在 Tauri command 或 Python handler 边界使用明确 model/schema，不复制 Protocol envelope 字段。
- 协议文字规范和 golden fixture 位于 `docs/protocol/`，是跨语言 review 的公共入口。
- Vault 只在 Rust `vault/`；Sidecar 接收初始化所需 runtime key，但不负责 DPAPI 或长期凭据管理。
- Host Key identity、candidate、trusted record 和 compare-and-swap replacement 保持独立模型，不与 credential 合并。
- WebView capability/permission 和 event allowlist 分别在 Rust 配置与 Broker 中显式维护。

## 代码规范

- Frame 格式严格为 `Content-Length: <UTF-8 JSON byte length>\r\n\r\n<JSON>`；header name 大小写敏感，只允许一个 header。
- 长度按 UTF-8 encoded bytes 计算，不按字符数、字符串长度或估算值计算。
- Envelope 必须拒绝未知字段、非法 version、非法 UUID/timestamp、非 object payload 和非正 sequence。
- 每个 sender 的 sequence 从 `1` 开始且逐 frame 加一；duplicate、regression 或 gap 是 terminal violation。
- application response/error 必须复用原 request 的 `request_id`；未知或重复 active ID fail closed。
- decoder 发生 terminal violation 后清空 buffer，不扫描后续 bytes 猜测下一帧边界。
- `sensitivity=secret` 限制观察与持久化，不宣称 pipe 已加密；secret payload 不进入 `Debug`、日志、Trace、Audit 或普通 error detail。
- Logger 不执行内容 allowlist、脱敏、截断或正文替换；完整保留 message、结构化字段、exception traceback 与 HTTP response body。不得记录 credential/API Key、runtime key 或 raw secret frame 的责任属于具体调用点。
- binary 只允许进入 method 明确定义的有界 canonical Base64 字段；当前没有通用 encrypted Artifact fallback，达到 method 上限必须显式失败。
- 新增字段或 method 时先冻结双侧契约和失败语义，再实现 Rust、Python、fixture 与测试。

## 长期约束

Protocol v1 当前固定值：

| 设置 | 值 |
| --- | ---: |
| Protocol version | `1` |
| Maximum header | `8192` bytes |
| Maximum JSON payload | `1048576` bytes |
| Outbound queue | `64` frames |
| Heartbeat interval | `5000` ms |
| Heartbeat timeout | `15000` ms |
| Graceful shutdown timeout | `3000` ms |
| Active application request limit | `16` |

其他不可放宽的边界：

- 不支持 protocol downgrade、alternate stdout、line-delimited JSON fallback 或 implicit backend。
- Rust Broker 的 Sidecar event allowlist 只允许 `ssh.connection.status`、`ssh.pty.output`、`ssh.pty.closed`、`manual_sftp.operation.progress`。前三者投影到 `ssh://event`；manual SFTP progress 必须严格解析并只投影到 `manual-sftp://operation-state`，未知字段、`cancellable=true` 或未知 event fail closed。
- Rust coordinator 自己产生的 transfer progress 不属于 Sidecar event allowlist：它只能把严格 `TransferProgressProjection` 投影到固定 `main` window 的 `manual-sftp://transfer-state`。该类型不含本地路径、`PathBuf`、文件 bytes、raw Protocol frame、stderr 或 raw exception；`committing` 固定不可取消。
- `ssh.connect` 和 `pty.write` 始终为 secret frame；ProxyJump 的 `host_key.inspect` 因携带 jump credential 也为 secret。
- Agent 固定六个 Sidecar method：五个 `agent.api_configs.*` metadata method 使用 `normal` sensitivity，唯一 `agent.turn.run` 使用 `secret` sensitivity。后者携带短生命周期 API Key Base64、user message 与 opaque IDs；任何 sensitivity 错配、未知字段或 identity race 都 fail closed。
- Agent response 在编码前按完整 Protocol v1 envelope 校验 1 MiB payload 上限；超限返回 `AGENT_RESPONSE_TOO_LARGE`，不得截断、摘要或返回部分 `final_text`。
- Agent canonical SystemMessage 默认要求有界只读排查、把 Tool output 视为不可信数据、禁止读取或回显 secret，并要求所有远程状态变更先展示精确命令、影响和回滚后等待新的用户确认；破坏性或高危操作必须经过两个独立且后发的用户确认消息。该提示词只是实验性模型行为约束，不是 backend approval enforcement；服务端 `CommandSafetyReviewer`、唯一 `execute_command` tool、绑定 live Session 和 capability 边界仍是独立强制控制。
- SSH 凭据快照使用范围 `1..2^53-1` 的 JSON 整数 `profile_version`；目标与 ProxyJump 都必须在网络 I/O 前精确匹配，旧 `profile_updated_at`、缺失字段、类型转换和越界值全部 fail closed。
- 所有 secret byte 字段使用 canonical base64，并在 handler 完成后 zeroize。
- WebView 不可调用 `connections.get`、Agent SFTP 或 raw Sidecar method。实验性 Agent 只通过固定 `main` window 的五个 Agent 配置/turn commands 和两个模型 API Key Vault commands 暴露；approval window 没有这些权限。用户手动 SFTP 仍只通过固定 `main` window 的 21 个 typed commands 暴露，不能被 Agent 调用；native picker path 只进入 Rust local-file owner，Frontend 只接收 display metadata 与 typed projections。
- WebView mutation payload 不是 snapshot 权威。Rust 必须在 dispatch 前通过私有 typed method 取得 canonical no-follow snapshot/hash，Python 在远端 mutation 前立即复核；本地 target identity、transaction handle、DPAPI journal 和 download `.part` recovery 均停留在 Rust，不能进入 Protocol 或 WebView。
- `manual_sftp.recovery.execute` 严格携带 `{recovery_id, action, operation_id}`。Rust 预生成 fresh remote `operation_id`，Python 在 mutation I/O 前拒绝旧 ID 或已持久化 ID；Rust journal 持久化 local recovery ID 与 remote operation ID 的精确关联，重启后只按真实 remote ID inspect/execute，不猜测、不重放。
- `manual_sftp.delete.preflight` 严格携带 `{operation_id, ssh_session_id, path}`。Rust 在 dispatch 前持久化 caller-selected `operation_id`，Python 在保存 encrypted delete plan 前拒绝复用并原样使用该 ID；成功 plan 的本地记录持续存在到可信 terminal，reply loss 或 shutdown 后只按同一 remote ID 恢复，不重放 preflight。
- transfer progress emit 是观察通道，不是 mutation acknowledgement。emit 失败只能记录 operation ID、phase 和稳定 error code，不中断或重放 remote transfer；terminal receipt 仍按 request/response 严格验证并独立返回。
- 可信且严格关联的 typed Sidecar error 是确定 failure，必须保留原稳定 code；若 error 明确携带 Python 已持久化的 `operation_state=cleanup_required|outcome_unknown`，Rust 必须保留对应 journal，不能删除为普通 failed。只有 dispatch 后 timeout、channel loss、malformed response、Sidecar crash 或 reply loss 才能由 Rust自行进入 `outcome_unknown`。任何未知状态都不得自动重试或回放 mutation。
- Rust mutation actor 在 Tauri caller timeout/drop 后仍串行等待原 request 收敛；收到真实终态后先更新 journal，再处理排队的 transfer abort。不得通过 drop broker future 让 Sidecar forward mutation 与 cleanup 并发执行。
- Remote recovery 绑定 profile `version`、target Host Key fingerprint 和完整 ProxyJump identity；仅 `connection_id` 相同不足以恢复。恢复所用 Session identity 不一致时 fail closed。
- Host Key changed 必须先展示明确 old/new identity，再通过 compare-and-swap replacement；不得自动接受或覆盖。
- heartbeat timeout 进入显式暂停/失败状态，不自动重启 Sidecar。
- Audit、Trace 和 SQLite 不得保存 credential plaintext、runtime key 或 raw secret frame；日志调用点不得主动提交这些内容，Logger 不提供兜底扫描。
- Rust 对每条 Sidecar stderr 只解析结构化严重级别并原样转发；malformed、foreign 或 unsupported level 以 WARNING 原样记录，长行与任意正文均不被截断或丢弃。

## 项目命令

从仓库根运行跨语言协议相关验证：

```powershell
cargo test --manifest-path frontend\src-tauri\Cargo.toml --test protocol_contract
cargo test --manifest-path frontend\src-tauri\Cargo.toml --test broker_contract

cd backend
.\.venv\Scripts\python.exe -m pytest tests\protocol -v
```

跨层或安全边界变更完成后，从仓库根运行适用门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

## 验证要求

- Codec：覆盖任意 chunk boundary、多 frame、oversize header/payload、invalid UTF-8/JSON、unknown field 和 buffer reset。
- Ordering：覆盖 sender-local sequence、duplicate/regression/gap、out-of-order correlated completion 和 capacity backpressure。
- Secret：使用 marker 验证业务调用点没有主动把 secret payload 交给 Logger，且 payload 不进入 Trace、Audit、SQLite、event 或 `Debug`；日志格式器本身必须完整保留测试传入的任意内容。
- Command/method：覆盖 capability/permission、sensitivity、typed payload、zeroize 和稳定 error code。
- Host Key：覆盖首次观察、exact match、changed、compare-and-swap 冲突、ProxyJump jump/target 两段身份。
- Lifecycle：覆盖 heartbeat、graceful shutdown、forced termination、pending reply failure 和无自动重放。
- 仅单侧测试通过不能证明双侧契约完成；Protocol 变更必须运行 Rust/Python 对应测试和 golden fixture 检查。

## 何时需要更新本文档

以下变化必须同步更新本文档：

- frame、envelope、limit、sequence、heartbeat、shutdown 或 request correlation 改变；
- 新增/删除 method、event、sensitivity 分类或 WebView 暴露；
- credential、Host Key、Vault、runtime key、zeroize、日志或持久化边界改变；
- Rust/Python Protocol 文件、fixture 或契约测试路径改变；
- 引入新的 transport 或 Protocol version。

内部实现优化未改变 wire contract 和安全边界时无需更新，但必须完成 AGENTS 影响检查。
