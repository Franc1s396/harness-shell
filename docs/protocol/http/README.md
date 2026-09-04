# Loopback HTTP / Runtime WebSocket v1

本目录是 React WebView 与 packaged Python Backend 之间当前 loopback 契约的公共入口。它只适用于同一台 Windows 设备上由 Launcher 启动的 Backend，不是 remote API。

## 契约文件

- `openapi-v1.json`：FastAPI 实际导出的 47 个 typed HTTP operations。
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

## Agent turn SSE

`POST /v1/agent/turns` 保持原 JSON request body，并要求以下 header：

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

合法序列只有 `started -> text_delta* -> completed -> EOF` 或 `started -> text_delta* -> failed -> EOF`。每个 stream 的 `sequence` 从 0 连续递增，request/conversation/run identity 固定；首版只公开最终 AI 文本 delta，不公开 reasoning、tool call、command、stdout/stderr、usage 或 Provider response metadata。不存在 JSON success、fallback parser、reconnect、resume 或 replay。

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
