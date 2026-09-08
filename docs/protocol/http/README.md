# Loopback HTTP / Runtime WebSocket v1

本目录是 React WebView 与 packaged Python Backend 之间当前 loopback 契约的公共入口。它只适用于同一台 Windows 设备上由 Launcher 启动的 Backend，不是 remote API。

## 契约文件

- `openapi-v1.json`：FastAPI 实际导出的 48 个 typed HTTP operations。
- `runtime-websocket-v1.schema.json`：单 owner Runtime WebSocket 的九类 strict text messages。
- `fixtures/problem-details-v1.json`：稳定 Problem Details 示例。
- `fixtures/limits-v1.json`：JSON、Agent SSE、WebSocket、PTY、SFTP、capacity、startup、heartbeat 与 shutdown 固定限制。
- `fixtures/agent/`：Agent HTTP/SSE 合法与非法边界。
- `fixtures/manual-sftp/`：Manual SFTP JSON/binary HTTP 合法与非法边界。

## 固定边界

- Python 只通过 `serve --port <1..65535>` 启动并固定监听 `127.0.0.1`。
- Launcher 通过 Backend `--port 0` 获得动态端口并拥有 packaged child/Windows Job；React 按 live → ready → WebSocket ping/pong 顺序建立 Runtime client。
- HTTP 使用 `X-Request-ID` 做 correlation；它不是 idempotency 或 replay key。
- credential envelope 只随 Connection 或 Provider mutation 提交；不存在独立 credential mutation route。`GET /v1/runtime/credential-encryption-key` 只发布进程临时公钥。
- PTY input 和 runtime/SSH/PTY/SFTP events 只走 `/v1/runtime/events`；HTTP 不提供 `pty.write`。
- Manual SFTP binary chunk 只用 `application/octet-stream`；不存在 Base64 binary route、generic RPC、alternate transport 或 fallback。
- 当前没有 HTTP authentication、TLS、remote bind、daemon 或 Windows Service 支持。

## Provider 上下文配置

`/v1/agent/api-configs` 的创建、更新、列表响应包含：

```json
{
  "context_window_size": 128000,
  "context_compaction_threshold_ratio": 0.75,
  "max_output_tokens": 8192
}
```

创建/更新省略字段采用上述默认值。token 字段必须为正 JS-safe 整数，比例为 0 与 1 之间的有限数，并满足 `1 <= floor(context_window_size * context_compaction_threshold_ratio) <= context_window_size - max_output_tokens`。UI 使用百分比编辑。此配置随本轮 Provider snapshot 冻结。

上下文不新增 endpoint 或 SSE event。开始后的摘要失败、预算超限、摘要持久化校验失败分别以现有 failed event 携带 `CONTEXT_COMPACTION_FAILED`、`CONTEXT_BUDGET_EXCEEDED`、`CONTEXT_SUMMARY_INVALID` 及 safe_message。`CONTEXT_TOKENIZER_UNAVAILABLE` 是初始化失败，阻止 Backend READY。数据库升级为 fresh-only v7，旧数据目录不会迁移。

## Agent turn SSE

`POST /v1/agent/turns` 接收 `conversation_id`、`ssh_session_id`、`api_config_id`、`user_message`，以及可选 `user_message_id`（UUID，默认 null）与 `retry`（严格 boolean，默认 false）。WebView 为每条新用户消息生成稳定 ID；重试必须携带同一 ID、原文及 `retry=true`。后端只替换该消息对应的最后终态尝试，创建新 Run，不追加重复用户消息或回放旧授权。身份冲突、正文改变、目标已不是末轮或仍运行返回 `AGENT_RETRY_CONFLICT`（409）。若该消息从未落库，则没有历史可删除，重新执行首次发送；若 started 丢失且 conversation_id 为 null，按稳定 ID 找回准确会话。该行为不撤销已执行的远程命令，也不恢复断开的 SSE。

请求要求以下 header：

```http
Accept: text/event-stream
Content-Type: application/json
X-Request-ID: 10000000-0000-4000-8000-000000000001
```

成功响应只有 `200 text/event-stream; charset=utf-8`，并携带匹配的 `X-Request-ID` 与 `Cache-Control: no-store`。每个 event 固定为三行 UTF-8 加一个空行，Backend 只输出 LF：

```text
event: agent.turn.started
id: 0
data: {"schema_version":1,"type":"agent.turn.started","request_id":"10000000-0000-4000-8000-000000000001","sequence":0,"conversation_id":"40000000-0000-4000-8000-000000000004","agent_run_id":"50000000-0000-4000-8000-000000000005","status":"RUNNING","react_iteration":0}

event: agent.turn.text_delta
id: 1
data: {"schema_version":1,"type":"agent.turn.text_delta","request_id":"10000000-0000-4000-8000-000000000001","sequence":1,"conversation_id":"40000000-0000-4000-8000-000000000004","agent_run_id":"50000000-0000-4000-8000-000000000005","delta":"完成"}

event: agent.turn.completed
id: 2
data: {"schema_version":1,"type":"agent.turn.completed","request_id":"10000000-0000-4000-8000-000000000001","sequence":2,"conversation_id":"40000000-0000-4000-8000-000000000004","agent_run_id":"50000000-0000-4000-8000-000000000005","status":"COMPLETED","react_iteration":0,"error_code":null}

```

合法序列只有 `started -> (text_delta | text_replace | tool_started | approval_requested | approval_resolved)* -> completed -> EOF` 或 `started -> (text_delta | text_replace | tool_started | approval_requested | approval_resolved)* -> failed -> EOF`。每个 stream 的 `sequence` 从 0 连续递增，request/conversation/run identity 固定；公开实时 AI 可见文本 delta 与完整 text_replace 更新，包括工具前说明，tool_started 和 approval_requested 公开已校验的工具调用 ID、工具名与完整 command 参数，approval_requested 另外携带冻结目标和审核原因；不公开 reasoning、stdout/stderr、usage 或 Provider response metadata。不存在 JSON success、fallback parser、reconnect、resume 或 replay。

HTTP 200 的启动边界是：request header/body、dispatcher capacity、Provider config/credential、conversation/SSH Session 全部校验完成，conversation lock 已取得、durable `RUNNING` Run 已创建、capacity 64 的 queue 已建立且 `started` 已安全入队。此前失败返回 Problem Details；此后失败先落 durable terminal Run，再通过唯一 `failed` event 结束。terminal frame 被 consumer 发送前，dispatcher request ID 与 capacity 仍保持占用；发送后 worker 收敛并以 clean EOF 结束。

单 frame encoded 上限为 65,536 bytes；单 turn SSE body 上限为 4,194,304 bytes，并为 terminal frame 保留 65,536 bytes；完整 Agent result 的逻辑预算仍为 1,048,576 bytes。所有上限均 fail closed，不截断、不合并、不切换 transport。

Agent turn SSE is scoped to the POST response that created the turn. It does not
use or extend the single-owner Runtime WebSocket; heartbeat, PTY, SSH state, and
Manual SFTP progress remain on that existing channel.

## 更新与验证

OpenAPI 与 WebSocket schema 由当前 Python routes/models deterministic 导出。修改 route、model 或 message union 后，先显式写回 artifact，再执行严格检查：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --write
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --check
backend\.venv\Scripts\python.exe -m pytest backend\tests\web\test_contract_artifacts.py -q
```

普通测试不得自动写回 artifact。任何 drift 必须直接失败并由开发者审查生成差异。

`agent.turn.text_replace` 携带 `text: string`，替换当前 provisional 内容；空字符串清空内容。它与 text_delta 共用严格关联、连续 sequence 和字节预算。大快照以有界替换首帧加后续增量编码，completed 后显示内容与最终消息一致。

### Agent 客户端取消

用户取消通过 AbortSignal 中止创建本轮的 POST SSE，不新增取消 endpoint 或 Runtime WebSocket 消息。Backend 在 HTTP 200 启动屏障前及流期间均处理 ASGI disconnect，并取消、等待 worker；启动前沿用 AGENT_CANCELLED Problem，已创建 Run 由既有取消路径落库，已完成 Run 不被改写。前端本地取消清除 provisional 内容并保留已知 conversation ID，不伪造服务端 terminal event；若终态已经验证，再取消等待 EOF 时保留该终态。其他网络、协议或 Problem 错误仍显式失败，正常读取仍要求 terminal 后 EOF。取消不撤销已执行的远程命令，也不保证远程进程已停止。

`agent.turn.tool_started` 携带公共关联字段、`tool_call_id`（1–1024 字符）、`tool_name: "execute_command"` 和 `arguments: {command: string}`（原始已校验命令，1–4096 字符，禁止 NUL/额外字段）。工具名、参数与安全审查通过后，Backend 在调用执行器前先将事件入队；发布失败不继续执行。该事件共用连续 sequence、字节预算和队列背压，不修改可见文本。UI 保留已有回复并显示“工具执行中…”，直到下一合法业务事件到达；连续 tool_started 继续显示，text_delta/text_replace 清除提示，终态、取消和断流也清除提示。因此工具结束后等待模型输出期间仍可能显示此提示。

UI 按本 turn 事件顺序累计工具记录，重复调用分别保留，文本替换不清空记录。成功完成后记录随 assistant 消息保存在当前页面内存中，并在完整回答下方显示默认折叠的“执行工具（数量）”；展开以纯文本 JSON 显示完整参数，无工具时不显示入口。记录表示进入执行器的调用尝试，不保证远程命令成功；校验或审查拒绝的调用不计入。失败、取消或断流丢弃当前临时列表，本次不提供这些终态的工具列表 UI 或历史恢复。仅此工具记录允许向 UI 传递 command；stdout/stderr、Provider 原始响应和凭据解析结果仍不进入事件或日志。


## Agent HITL

新增 `POST /v1/agent/approvals/{approval_id}/decision` 和 `agent.turn.approval_requested`、`agent.turn.approval_resolved` 两种原 turn SSE 事件；精确字段见 OpenAPI。审核没有 expires_at、TTL 或超时决定。决定成功只确认记录，原 worker 通过 Command(resume=...) 继续；approve 消费一次后派发，reject 写工具拒绝结果并继续模型。普通容量 16，独立审核控制容量 1，共用请求 ID 与 shutdown。错误和身份边界见 [Protocol & Security](../../agents/protocol-security.md#agent-审核协议)。
