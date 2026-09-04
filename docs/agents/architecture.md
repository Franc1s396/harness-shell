# Architecture Guide

## 适用范围

跨进程架构、模块归属、状态权威、生命周期或跨层调用链变化时必须读取本文档，并同时读取受影响层的领域文档。

## 当前进程模型

Harness Shell 的生产桌面路径只有一条：

```text
NSIS shortcut / finish action
  -> harness-shell-launcher.exe
  -> harness-shell-sidecar.exe desktop --port 0 --data-dir <absolute> --control-read-handle ... --ready-write-handle ...
  -> Backend binds 127.0.0.1:<dynamic> and writes one bounded ready frame
  -> harness-shell-ui.exe --backend-url http://127.0.0.1:<dynamic>
  -> React direct typed HTTP (including Agent turn SSE) + one Runtime WebSocket
```

Launcher 独占两个 child、Windows Job、ready/control pipe、启动顺序和退出清理。它不扫描端口、不 reconnect、不 respawn。Backend 提前退出时 UI 不被重新绑定；UI 退出时 Launcher 发送一次 graceful byte，3 秒后仍存活则终止 Job。

开发模式显式拆分为 Backend 与 UI：

```powershell
backend\.venv\Scripts\python.exe -m harness_shell_sidecar serve --port 8765 --data-dir E:\absolute\dev-data
npm.cmd --prefix frontend run tauri:dev -- -- --backend-url http://127.0.0.1:8765
```

## 权威与禁止边界

| 组件 | 独占权威 | 不得承担 |
| --- | --- | --- |
| Launcher | packaged child、Job、动态端口协商、ready/control pipe、退出顺序 | 业务 API、凭据解析、状态机、端口扫描 |
| Tauri UI shell | `get_backend_bootstrap`、主窗口关闭/销毁权限 | HTTP/WebSocket 代理、Backend 生命周期、业务状态、文件传输、approval UI |
| React | UI 状态、typed loopback client、Agent SSE framing/语义验证/临时文本、Runtime WebSocket、连接私钥 picker/读取、Manual SFTP 本地 picker/handle/SHA-256/256 KiB chunk loop | SSH/PTY 远端状态、任意本地路径 API、后台恢复 |
| Python Backend | FastAPI lifespan、dispatcher、Agent SSE worker/startup barrier/queue、SQLite、凭据、SSH/PTY、remote SFTP、Agent、结构化日志 | 本地 file picker/handle、Desktop child/Job、自动重连/重放 |

React 在启动时只通过 Tauri bootstrap command 取得固定 loopback base URL；之后业务调用直达 Python。当前没有独立 approval window、approval HTTP route 或审批状态，Agent 首轮风险确认仍由 React 在发送前显式执行。不得重新引入 Rust 业务代理或 generic RPC。

## 持久化与 Manual SFTP

Runtime SQLite 只接受全新 schema v6。检测到旧 schema 必须在任何写入或 WAL 配置前失败；没有自动迁移、兼容读取或导入。schema v6 使用 plaintext JSON/列存储，凭据、Agent message/output、remote recovery 等可能明文落盘。没有 SQLite Audit/Trace 表；诊断只写 Python 日志目录。

连接与 Provider 凭据没有独立 mutation route。React 使用 Runtime 公钥加密用户输入，并分别随 `/v1/connections` 或 `/v1/agent/api-configs` 的创建/更新请求提交；Python handler 在同一 SQLite 事务内创建、替换或删除业务记录及其拥有的凭据。连接私钥由 React 文件选择器读取，Backend 永远不接收本地路径。

Manual SFTP 只允许固定 typed JSON endpoints 加严格 raw chunk endpoints。React 负责本地选择、同步 save picker、handle 生命周期、hash 和 chunk iteration；页面 reload 会丢失本地 preparation。Python 负责远端 snapshot 复核、temporary path、commit、abort 和 recovery record。不得把本地绝对路径发送给 Backend，不提供可恢复的本地 download-part 状态。

## Agent turn 流式调用链

React `BackendHttpClient` 通过 `fetch()` 消费创建本轮的 POST response `ReadableStream` 并独占 SSE framing/UTF-8/byte budget；`api/agent.ts` 独占 event schema、sequence 和 correlation 状态机；per-tab reducer 只暂存 provisional text，收到合法 completed 后才提交 assistant message，任何 failed/interrupted 都丢弃 partial text。

Python `AgentTurnStreamSession` 在 shared dispatcher 内拥有 worker、capacity 64 queue、HTTP-200 startup barrier 与断连收敛；terminal frame 被 consumer 发送前不会释放 dispatcher request ID/capacity。`AgentTurnApplication` 继续拥有 Provider snapshot、credential resolve/zeroize；`AgentService` 拥有 durable RUNNING/terminal 顺序；`ModelGateway` 只向 sink 发布最终回答调用的可见文本。该 POST stream 不使用或扩展 Runtime WebSocket。

## 失败语义

- 未知字段、非法 enum/encoding、错误 correlation、越界 payload、重复 owner 和旧 schema 全部 fail closed。
- 不得用 fallback、兼容路由、重试切换、响应猜测或后处理伪造成功。
- HTTP pre-stream failure 使用 Problem Details；Agent POST 启动后使用 strict SSE terminal failure。Runtime WebSocket 仍为单 owner，queue bounded，不 drop/merge。
- 自动测试、构建、package、SSH Lab、Desktop 人工验收和生产验收必须分别陈述。

## 文档同步

进程所有权、调用链、API/WebSocket、持久化、安全边界、命令或验收范围变化时，同步更新本文件与对应领域文档；局部实现细节不复制成第二真源。
