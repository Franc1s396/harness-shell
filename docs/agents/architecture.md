# Architecture Guide

## 何时必须读取

出现以下任一情况时必须读取本文档：

- 新增或修改跨 WebView、Rust Core、Python Sidecar 的功能；
- 判断模块、状态、数据或错误应由哪一层负责；
- 修改 Tauri command、typed HTTP/WebSocket request/response/event 或 Sidecar handler；
- 讨论 M2/M3 已实现范围、后续设计范围或真实验收边界。

涉及协议、凭据或事件暴露时，还必须读取 [Protocol & Security Guide](protocol-security.md)。

## 范围与职责

Harness Shell 的当前主调用链为：

```text
User → React WebView → typed API wrapper → Tauri command
     → Rust Core / AppState / RuntimeClientHandle / Vault
     → loopback typed HTTP 或 single-owner Runtime WebSocket
     → FastAPI/Uvicorn → RequestDispatcher / Domain Service
     → typed response 或 allowlisted event → Rust projection → WebView
```

各层权威如下：

| 层 | 拥有的职责 | 不得承担的职责 |
| --- | --- | --- |
| React WebView | 展示、用户交互、非敏感 UI 状态、typed API 调用 | 凭据持久化、原始协议处理、任意 shell、Sidecar 进程管理 |
| Tauri Rust Core | command 暴露、DPAPI Vault、packaged child/Job、dynamic port、RuntimeClientHandle、敏感参数注入、事件白名单 | 业务展示状态、让 WebView 直连 Python、复制 Python 领域状态机 |
| Python Sidecar | typed HTTP/WebSocket 边界、SSH/PTY 生命周期、契约授权的用户手动 SFTP remote I/O/mutation、实验性 ReAct Agent 与 Provider 调用、Runtime SQLite、Audit、Trace | WebView UI、DPAPI Vault、远程监听、未经契约授权的远程写操作 |
| Remote SSH host | 被显式连接和操作的远程资源 | 本地状态权威或隐式重放来源 |

## 当前源码真源

- WebView 入口与工作区控制：[frontend/src/App.tsx](../../frontend/src/App.tsx)、[frontend/src/features/workspace/WorkspaceController.tsx](../../frontend/src/features/workspace/WorkspaceController.tsx)
- WebView typed API：[frontend/src/api/](../../frontend/src/api/)
- Frontend Agent typed API 与状态编排：[frontend/src/api/agent.ts](../../frontend/src/api/agent.ts)、[frontend/src/features/agent/](../../frontend/src/features/agent/)
- Rust 应用状态：[frontend/src-tauri/src/app_state.rs](../../frontend/src-tauri/src/app_state.rs)
- Tauri command 注册：[frontend/src-tauri/src/lib.rs](../../frontend/src-tauri/src/lib.rs)、[frontend/src-tauri/src/commands/](../../frontend/src-tauri/src/commands/)
- Rust Agent command 与模型凭据边界：[frontend/src-tauri/src/commands/agent.rs](../../frontend/src-tauri/src/commands/agent.rs)、[frontend/src-tauri/src/commands/credentials.rs](../../frontend/src-tauri/src/commands/credentials.rs)
- Rust Runtime client、WebSocket、process/Job 与 production supervisor：[frontend/src-tauri/src/runtime/](../../frontend/src-tauri/src/runtime/)
- Rust 用户手动 SFTP coordinator、模型、私有 wire 与本地 journal：[frontend/src-tauri/src/sftp/](../../frontend/src-tauri/src/sftp/)
- Frontend 用户手动 SFTP typed API、controller 与 workspace：[frontend/src/api/manual-sftp.ts](../../frontend/src/api/manual-sftp.ts)、[frontend/src/features/sftp/](../../frontend/src/features/sftp/)
- Python Runtime：[backend/src/harness_shell_sidecar/runtime/](../../backend/src/harness_shell_sidecar/runtime/)
- Python 业务 handler：[backend/src/harness_shell_sidecar/connections/](../../backend/src/harness_shell_sidecar/connections/)、[backend/src/harness_shell_sidecar/ssh/](../../backend/src/harness_shell_sidecar/ssh/)、[backend/src/harness_shell_sidecar/terminal/](../../backend/src/harness_shell_sidecar/terminal/)、[backend/src/harness_shell_sidecar/manual_sftp/](../../backend/src/harness_shell_sidecar/manual_sftp/)、[backend/src/harness_shell_sidecar/agent/](../../backend/src/harness_shell_sidecar/agent/)
- 持久化真源：[backend/src/harness_shell_sidecar/storage/](../../backend/src/harness_shell_sidecar/storage/)
- HTTP/WebSocket 契约：[docs/protocol/http/](../protocol/http/)

## 项目结构规范

- UI 展示和非敏感交互状态放在 `frontend/src/`，不得把 Rust/Python 权威复制成第二套业务真源。
- WebView 与本地能力的边界统一经过 `frontend/src/api/` 和已注册的 Tauri command。
- 凭据、进程生命周期、特权暴露与事件过滤放在 `frontend/src-tauri/`。
- 跨进程模型分别位于 Rust `runtime/` 与 Python `web/`；`docs/protocol/http/` 保存 deterministic OpenAPI、WebSocket schema、limits 与 fixtures。
- 本地诊断链固定为 Python Sidecar 完整单行 JSON stderr → Rust Sidecar 进程 owner 仅解析严重级别并原样转发 → `tauri-plugin-log` 同时写启动终端和 Tauri `LogDir`；两层都不扫描、截断或脱敏日志正文，WebView 不接收 stderr 或日志内容。
- SSH、PTY、远程 I/O、runtime storage、Audit 和 Trace 放在 Python Sidecar 的明确子包中。
- 新模块按“谁拥有状态和失败责任”落位，不按调用方便、当前 import 方向或临时复用落位。
- 跨层数据先定义严格契约，再分别实现 transport、handler、projection 和验证；不得让未建模的字典或字符串穿透所有层。

## 代码规范

- WebView 通过 typed wrapper 调用 Tauri，不在组件中散落 `invoke`、事件名和 payload 拼装。
- Rust Core 对 WebView 输入和 Sidecar 输出都执行边界验证，只投影允许公开的数据。
- 用户手动 SFTP transfer progress 由 Rust coordinator 持有的 typed sink 投影到固定 `main` window 的 `manual-sftp://transfer-state`；payload 只能是 `TransferProgressProjection`，不得包含本地路径、文件内容或 raw transport/error。
- Python HTTP routes 把边界输入转换为 transport-independent `RequestContext + Mapping`；Manual SFTP binary route 和 WebSocket PTY input 通过同一个 dispatcher request owner 进入 raw-byte application boundary。
- Python `runtime/resources.py` 是初始化后数据库、Connection/SSH/PTY/Manual SFTP/Agent、Audit、Trace、可清零 key buffer 与 dispatcher 的唯一 owner；ASGI lifespan 不得复制领域资源初始化或清理顺序。
- `web/app.py` 当前提供无 import-time DB/key/background side effect 的 FastAPI factory；lifespan `RuntimeOwner` 独占一次性 initialize、状态、single-owner Runtime WebSocket gateway 与 shutdown，Runtime、Connection、Host Key、SSH、PTY control、Agent 和 Manual SFTP routes 只执行 strict HTTP validation、request correlation、共享 dispatcher 调用与 typed error/result 映射。Manual SFTP binary routes 与 WebSocket PTY input 调用 raw-byte application boundary，但仍由同一 dispatcher 独占 duplicate/capacity/cancel 生命周期。
- 进程、SSH connection、PTY channel、async task 和数据库连接必须有唯一 owner 和显式关闭路径。
- 跨层错误使用稳定 error code 和安全 message；原始异常、stderr、secret payload 不穿透到 WebView。
- 复杂调用链应注释状态转换、失败点、取消语义和资源所有权，避免只解释语法。

## 长期约束

- 架构保持 Tauri 2 + React/TypeScript + Python Sidecar，除非用户明确批准新的架构决策。
- Rust Core 是凭据、安全和 Sidecar 进程边界；Python Sidecar 不能直接向 WebView 暴露能力。
- Python process 入口只接受 `serve --port <1..65535>`，并用 Uvicorn 固定监听 `127.0.0.1`；不支持 remote bind、auth/TLS、generic RPC、stdio 或兼容 fallback。
- `docs/protocol/http/` 是 HTTP/WebSocket/Problem/limits cross-language artifact 真源；`backend/scripts/export_http_contract.py --check` 从实际 FastAPI app 与 Pydantic WebSocket union 做逐字节一致性检查。Rust `runtime/` 独占固定 loopback base URL、禁用 proxy/redirect 的 sealed typed HTTP client、strict WebSocket actor、dynamic-port packaged process、Windows Job 和 production lifecycle。`RuntimeClientHandle` 在握手发布前或失败撤销后以 `RUNTIME_NOT_READY` fail closed；Connection、Host Key、SSH、PTY control、Agent 与 Manual SFTP 只走 sealed typed requests，PTY input 只走 Runtime WebSocket。Manual SFTP upload 直接发送 octet-stream，download 只向 coordinator 暴露已校验 identity/offset/length/EOF 的 bytes。
- SQLite 保存持久化 runtime facts；live session ID、SSH channel、PTY process 和内存 task 不是可重放持久状态。
- Runtime SQLite 当前为 schema v4。Connection profile 的持久化权威仍是单调 `version`：创建为 `1`，每次成功更新原子 `+1`，上限为 `2^53-1`；Rust 在解析凭据后把目标与 ProxyJump 的快照作为数字 `profile_version` 发送，Sidecar 在任何网络 I/O 前校验，`updated_at` 只用于展示。
- Sidecar 崩溃、heartbeat 超时或 HTTP/WebSocket contract failure 必须显式进入 `FAILED`，不得 reconnect、respawn 或重放请求。
- 旧的宽泛 Agent exec/SFTP/Artifact 运行时已删除。实验性 React Agent UI 只经 `list/create/update/delete_model_api_config`、`store/delete_model_api_key` 与 `run_agent_turn` 七个固定 Tauri commands 接入。每次发送先刷新 Provider 列表，在前端 Run projection 中冻结 Provider 展示快照与当时的 connected `ssh_session_id`，再由 Rust/Sidecar 在 `agent.turn.run` 中重复验证权威配置和 Session；模型唯一工具仍是严格 `execute_command`。命令使用该 Session 的隔离 non-PTY exec channel，不自动重试、不接入 SFTP/Artifact；七个 commands 仅授予固定 `main` window。
- React 只拥有 per-terminal-tab 的内存草稿、conversation id、首轮风险确认、non-streaming Promise 状态与安全结果投影；Sidecar 仍拥有 conversation/run/message 持久化和模型执行权威，Rust 仍独占 API Key。运行中的 Provider/Session 不因切换 tab、折叠 Agent 或编辑配置而迁移；所属 Session 的 close/disconnect 在任何 PTY/SSH cleanup 前被前端双层门禁拒绝。
- 用户手动 SFTP 的私有 Rust coordinator/wire、Python remote I/O/mutation、仅限固定 `main` window 的 21 个 typed Tauri commands/单一 `sftp` permission，以及非持久化 typed Frontend API/controller/workspace 已完成实现。SFTP Activity 在进入时只绑定当时显式选中的 connected terminal tab，并保持该绑定直到离开后重新进入；切换普通 active tab 不迁移 listing 或 transfer owner。进入时隐藏 Agent pane 但保留其偏好。该能力永远不得成为 Agent 工具、approval window 或 WebView raw Protocol 能力。
- Agent conversation、run 与 LangChain message 顺序由 Sidecar schema v4 持久化；message content、Tool Call 参数和原始 stdout/stderr 进入绑定身份的 AES-GCM encrypted record，现有业务调用点不主动把它们作为日志字段。统一 Logger 不提供内容过滤。模型上下文只取 system message 与最近二十轮 human turn；未匹配 Tool Call 在下一次模型调用前以 `PREVIOUS_TOOL_CALL_INTERRUPTED` 结构闭合，不使用 LangGraph checkpointer。
- Agent turn 在 per-conversation lock 内再次比对 handler 冻结的完整 Provider config，并确认绑定 `ssh_session_id` 仍存在于 connected Session registry；任一权威变化都必须在创建 Run 和模型调用前失败。成功 Run 只有在完整 HTTP JSON response budget 校验通过后才能持久化为 `COMPLETED`。
- WebView 只表达路径、名称和显式决策；Rust 在 mutation dispatch 前取得 canonical no-follow snapshot/hash，并独占 native picker、本地 handle、transfer bytes、DPAPI journal、下载 `.part` 重启检查和 disconnect/application-exit transfer gate；Python 对远端 snapshot 立即复核并拥有实际 SFTP mutation。任一层不得把 WebView 显示数据提升为文件身份权威。
- transfer progress sink 失败只产生调用点显式构造的本地诊断，不得中断、重试或改变已 dispatch 的远程操作；可信 terminal 仍由 coordinator workflow 返回，不能伪造成 progress phase。
- transfer command future 在 remote begin 后被丢弃时，Rust mutation owner 必须继续执行 detached abort，并把 remote abort、本地 `.part` 清理和 journal 收敛作为独立 cleanup owner 纳入 shutdown drain；不得因原 command future 已结束而提前报告 drained。
- 本地诊断的跨层所有权不改变业务权威：Python 产生完整结构化日志，Rust 原样转发并独占持久化文件、轮转和目录打开能力，Frontend 只显示 Rust 解析出的固定日志目录。
- 自动测试、容器实验室和桌面验收分别陈述，不能推导出生产部署、生产主机或迁移已经验收。

## 项目命令

架构变更至少运行所涉及层的子系统验证；跨层契约变更优先使用仓库门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

具体 Frontend、Rust、Python 和测试命令见对应领域文档。

## 验证要求

- 单层改动：运行该层最小相关测试，并说明未覆盖的其他层。
- Tauri command 或事件改动：验证 Frontend wrapper、Rust command 注册、capability/permission 和 Rust 契约测试。
- HTTP/WebSocket 契约改动：验证 Rust/Python 模型、artifact/fixture、边界值、错误路径和请求关联。
- 生命周期改动：验证正常关闭、取消、超时、异常退出、资源回收和重复操作。
- 跨层功能：在局部测试后运行适用的 M1/M2 门禁；需要人工桌面行为时另做 Tauri 桌面验收。

## 何时需要更新本文档

出现以下变化时，在同一代码变更中更新本文档：

- 新增、删除或移动跨层模块；
- 状态权威、进程边界、调用链或资源 owner 改变；
- 新增 transport、Provider、Agent Workflow 或远程写能力；
- M2/M3 能力边界或验收状态改变；
- 本文列出的源码真源路径失效。

仅修改某层内部实现且不改变上述长期事实时，无需更新本文档，但任务结束仍要报告已完成 AGENTS 影响检查。
