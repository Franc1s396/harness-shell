# Protocol & Security Guide

## 何时必须读取

出现以下任一情况时必须读取本文档：

- 修改 Rust/Python typed HTTP model、route、WebSocket message、request correlation 或 event；
- 新增 Tauri command、Sidecar handler 或跨进程数据字段；
- 修改凭据、Host Key、DPAPI Vault、runtime key、日志、Trace、Audit 或持久化；
- 修改 WebView capability/permission、事件白名单或 approval 边界；
- 处理 payload 越界、child crash、heartbeat timeout 或 decoder failure。

跨层设计同时读取 [Architecture Guide](architecture.md)，验证范围同时读取 [Testing Guide](testing.md)。

## 范围与职责

Rust Core 与 packaged Python child 之间唯一受支持的跨进程契约是 loopback-only typed HTTP API 与一个 typed Runtime WebSocket：

- Rust 独占动态端口、packaged child、Windows Job、DPAPI Vault、runtime key 注入和 WebView 权限；
- Python 固定监听 `127.0.0.1`，严格验证 HTTP/WebSocket 与 application payload，并管理 runtime secret 的最短生命周期；
- WebView 不获得 Python base URL、raw HTTP/WebSocket、runtime key、credential、local path、stderr 或任意 shell 能力；
- 任何 contract、安全不变量或不可信持久化失败都 fail closed，不降级、不猜测、不 retry mutation、不 reconnect、不 respawn。

当前 loopback API 不实现 authentication、TLS、Bearer Token、Cookie、OAuth、mTLS、remote access、daemon 或 Windows Service。此边界依赖 Rust 对 child、端口、环境与 WebView capability 的独占管理，不能被描述为可远程部署的通用 Web API。

## 当前源码真源

- OpenAPI、WebSocket schema、limits 与 fixtures：[docs/protocol/http/](../protocol/http/)
- Python FastAPI/Problem/limits/routes/WebSocket：[backend/src/harness_shell_sidecar/web/](../../backend/src/harness_shell_sidecar/web/)
- Python Runtime owner/dispatcher：[backend/src/harness_shell_sidecar/runtime/](../../backend/src/harness_shell_sidecar/runtime/)
- Rust typed HTTP/WebSocket/process/Job/supervisor：[frontend/src-tauri/src/runtime/](../../frontend/src-tauri/src/runtime/)
- Rust Tauri boundary：[frontend/src-tauri/src/commands/](../../frontend/src-tauri/src/commands/)
- Rust Manual SFTP coordinator/client：[frontend/src-tauri/src/sftp/](../../frontend/src-tauri/src/sftp/)
- Rust Vault：[frontend/src-tauri/src/vault/](../../frontend/src-tauri/src/vault/)
- Python secret/storage：[backend/src/harness_shell_sidecar/ssh/auth.py](../../backend/src/harness_shell_sidecar/ssh/auth.py)、[backend/src/harness_shell_sidecar/storage/](../../backend/src/harness_shell_sidecar/storage/)
- Python structured stderr 与 Rust process owner：[backend/src/harness_shell_sidecar/telemetry/logging.py](../../backend/src/harness_shell_sidecar/telemetry/logging.py)、[frontend/src-tauri/src/runtime/process.rs](../../frontend/src-tauri/src/runtime/process.rs)
- Cross-language tests：[backend/tests/web/](../../backend/tests/web/)、[frontend/src-tauri/tests/](../../frontend/src-tauri/tests/)

## HTTP 契约

- Python 唯一启动参数为 `serve --port <1..65535>`；host 固定为 `127.0.0.1`，禁止 host override、proxy headers、access log、server/date headers、自动换端口与 generic RPC。
- Rust 只构造 sealed request types；base URL 固定为 Rust 选择的 loopback port，HTTP client 禁用 proxy 与 redirect。
- 每个 operation 使用 `X-Request-ID` 关联；它不是 idempotency/replay key。missing/invalid ID 返回新的安全 correlation ID，不能回显非法 header 原文。
- JSON request/response encoded 上限均为 `1_048_576` bytes；Python active application request 上限为 16。duplicate active ID 返回 409，capacity 返回 429。
- 成功响应必须匹配 exact status、media type、request ID 与 strict response model；错误使用 strict `application/problem+json`。未知字段、重复 correlation header、错误 status/media type 或 malformed body fail closed。
- HTTP mutation 与 response-loss 不自动 retry、replay、猜测成功或隐式 cancel。
- Runtime key 精确为两个 canonical Base64 32-byte 值，只在一次性 `/v1/runtime/initialize` request 中注入 Python 内存；initialize body 的 Rust `Debug` 必须脱敏并在 drop 后清零。

## Runtime WebSocket 契约

- 路径固定为 `/v1/runtime/events`，每个 Python Runtime 只允许一个 active owner；第二个 owner 以固定 close code 拒绝，不能抢占。
- 双向只接受 strict UTF-8 text JSON，encoded 上限 `65_536` bytes；inbound/outbound queue capacity 均为 64，满时 backpressure，不 drop、不 merge。
- message type 精确为 `pty.input`、`pty.input_result`、`pty.output`、`pty.closed`、`ssh.connection_state`、`sftp.operation_progress`、`runtime.ping`、`runtime.pong`、`runtime.error`。
- heartbeat interval 为 5 秒，timeout 为 15 秒；只有 correlated `runtime.ping`/`runtime.pong` 刷新 liveness，业务消息不能隐式证明存活。
- PTY input canonical Base64 decode 后为 `1..32_768` bytes；result 必须匹配 causation、PTY identity 和 accepted byte count。PTY output sequence 按 PTY session 严格连续。
- unknown type/field、非法 UUID/timestamp/Base64/causation/sequence/size、binary/control data message 或 unexpected close 使 Rust Runtime 进入 `FAILED`，撤销 client，并尝试 bounded graceful shutdown 后终止 Job child。
- Rust 只把 SSH/PTY 投影到 `ssh://event`，把 Python SFTP progress 投影到 `manual-sftp://operation-state`；raw payload 和错误正文不进入 WebView。

## Manual SFTP 与 Agent 边界

- Manual SFTP metadata/control 使用 typed JSON；upload/download chunk 使用 `application/octet-stream`，单 chunk 为 `1..262_144` bytes。
- Upload request 与 download response 必须严格校验 operation、sequence、offset、byte count、EOF 和 media type；不存在 Base64 binary route 或兼容 adapter。
- Rust coordinator 独占本地 path/handle、native picker、DPAPI journal、download `.part`、TxF commit 与 shutdown drain；Python 独占 remote SFTP channel、snapshot 复核和 mutation。
- `cleanup_required`、`outcome_unknown`、fresh operation ID、no-clobber 与 recovery 语义保持显式；timeout/drop/reply-loss 后不得自动重放 mutation。
- transfer progress 是观察通道，不是 mutation acknowledgement；emit 失败不能改变已派发远程操作的结果。
- Agent API Key 只在 Rust DPAPI Vault；Python metadata 只保存 opaque secret reference。turn request 在 Rust `Debug` 中脱敏，使用后清零。
- Agent turn 固定 non-streaming，冻结完整 Provider config、credential identity、connected SSH Session 与 user message；只允许严格 `execute_command`，不接入 Manual SFTP、Artifact 或任意兼容 route。
- Agent response 按最终紧凑 HTTP JSON body 执行 1 MiB budget；超限返回 `AGENT_RESPONSE_TOO_LARGE`，不截断、不摘要、不返回部分文本。

## 凭据、日志与持久化

- SSH credential snapshot 使用范围 `1..2^53-1` 的 JSON integer `profile_version`；目标与 ProxyJump 都必须在网络 I/O 前精确匹配，`updated_at` 只用于展示。
- Host Key candidate、trusted record 与 compare-and-swap replacement 保持独立；changed 必须展示 old/new identity，不自动接受或覆盖。
- 调用点不得把 credential、API Key、runtime key、request/response body、user message、command、model response、stdout/stderr 或 SFTP bytes 交给 Logger、Trace、Audit、event、`Debug` 或普通 error detail。
- Logger 不做扫描、脱敏、截断或正文替换。Python 完整结构化 stderr 由 Rust 仅解析 severity 后原样持久化；WebView 不读取日志内容。
- SQLite、Audit 与 Trace 不得保存 credential plaintext、runtime key 或未授权明文；secret byte buffer 使用后 zeroize。
- WebView 只通过固定 main-window typed commands 使用 SSH、Terminal、Manual SFTP 与 Agent；approval window 不获得这些权限。

## 项目命令

从仓库根运行契约与边界验证：

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\web -q
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --check
cargo test --manifest-path frontend\src-tauri\Cargo.toml --test runtime_models_contract --test runtime_http_contract --test runtime_websocket_contract --test runtime_supervisor_contract
cargo test --manifest-path frontend\src-tauri\Cargo.toml --test runtime_command_contract --test manual_sftp_runtime_contract --test capability_contract --test vault_contract
```

跨层或安全边界变更完成后运行适用门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

自动测试、packaged backend、containerized OpenSSH、Tauri Desktop、真实 Provider、production SSH、authentication/TLS 与 deployment/migration acceptance 必须分别报告，不能相互替代。

## 何时需要更新本文档

以下变化必须同步更新本文档：

- HTTP route/model/limit、WebSocket message/heartbeat、shutdown 或 request correlation 改变；
- 新增/删除跨进程 operation、event 或 WebView 暴露；
- credential、Host Key、Vault、runtime key、zeroize、日志或持久化边界改变；
- Rust/Python contract 文件、fixture 或测试路径改变；
- 引入新的 transport、authentication、TLS 或 remote access。

内部实现优化未改变 wire contract 和安全边界时无需更新，但必须完成 AGENTS 影响检查。
