# Loopback HTTP / Runtime WebSocket v1

本目录是 React WebView 与 packaged Python Backend 之间当前 loopback 契约的公共入口。它只适用于同一台 Windows 设备上由 Launcher 启动的 Backend，不是 remote API。

## 契约文件

- `openapi-v1.json`：FastAPI 实际导出的 47 个 typed HTTP operations。
- `runtime-websocket-v1.schema.json`：单 owner Runtime WebSocket 的九类 strict text messages。
- `fixtures/problem-details-v1.json`：稳定 Problem Details 示例。
- `fixtures/limits-v1.json`：JSON、WebSocket、PTY、SFTP、capacity、startup、heartbeat 与 shutdown 固定限制。
- `fixtures/agent/`：Agent HTTP 合法与非法边界。
- `fixtures/manual-sftp/`：Manual SFTP JSON/binary HTTP 合法与非法边界。

## 固定边界

- Python 只通过 `serve --port <1..65535>` 启动并固定监听 `127.0.0.1`。
- Launcher 通过 Backend `--port 0` 获得动态端口并拥有 packaged child/Windows Job；React 按 live → ready → WebSocket ping/pong 顺序建立 Runtime client。
- HTTP 使用 `X-Request-ID` 做 correlation；它不是 idempotency 或 replay key。
- credential envelope 只随 Connection 或 Provider mutation 提交；不存在独立 credential mutation route。`GET /v1/runtime/credential-encryption-key` 只发布进程临时公钥。
- PTY input 和 runtime/SSH/PTY/SFTP events 只走 `/v1/runtime/events`；HTTP 不提供 `pty.write`。
- Manual SFTP binary chunk 只用 `application/octet-stream`；不存在 Base64 binary route、generic RPC、alternate transport 或 fallback。
- 当前没有 HTTP authentication、TLS、remote bind、daemon 或 Windows Service 支持。

## 更新与验证

OpenAPI 与 WebSocket schema 由当前 Python routes/models deterministic 导出。修改 route、model 或 message union 后，先显式写回 artifact，再执行严格检查：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --write
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --check
backend\.venv\Scripts\python.exe -m pytest backend\tests\web\test_contract_artifacts.py -q
```

普通测试不得自动写回 artifact。任何 drift 必须直接失败并由开发者审查生成差异。
