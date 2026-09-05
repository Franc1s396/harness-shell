# Protocol & Security Guide

## 信任边界

- Launcher 是 Desktop child、Windows Job、动态端口和 control/ready handles 的唯一 owner。
- Tauri shell 只把经过严格校验的 loopback base URL 交给主窗口，并保留主窗口关闭/销毁权限；不存在独立 approval window。
- React 是固定 loopback HTTP/WebSocket client；它不是任意网络、shell 或 filesystem gateway。
- Python Backend 是凭据、SQLite、SSH/PTY、remote SFTP 与 Agent 的业务 owner。

Backend 只监听 `127.0.0.1`，没有远程监听、TLS 或用户认证。loopback 不等于强认证边界：同一用户会话内其他进程可能访问端口。当前设计依赖随机动态端口、短进程生命周期、strict contract、request envelope 与最小 Tauri capability；不得把它描述为抵御同用户恶意进程的完整隔离。

## Desktop 控制协议

- Backend ready frame 走匿名 inherited pipe：4-byte length prefix + bounded strict JSON `{version, instance_id, port}`。
- Launcher 只接受 version 1、合法 UUID、nonzero port、无未知/重复字段；不得扫描监听端口。
- Launcher 到 Backend 的 graceful control 是一次单字节信号；EOF/非法内容/提前退出都按失败处理。
- Launcher 只把 Backend stderr 排入 per-user 独立轮转文件，不得把 stderr 内容转发给 Tauri、WebView 或 native error dialog；Tauri 自身日志与 Backend 日志使用不同文件。
- UI 只接受唯一 `--backend-url http://127.0.0.1:<nonzero>`；release 缺失 bootstrap 不允许独立运行。

## HTTP/WebSocket

- HTTP 只允许固定 `/v1/...` typed routes、`X-Request-ID`、bounded body/response 与 Problem Details。
- `POST /v1/agent/turns` 的 success 只允许 strict SSE。durable Run 之前的失败为 Problem Details；HTTP 200 后只允许 correlated `started -> text_delta* -> completed|failed -> EOF`，且只暴露最终 AI 可见文本。已知领域失败的 `message` 必须来自异常产生点明确审查的 `safe_message`，未知异常只能使用固定安全文本。
- Agent SSE 由创建本轮的 POST response 独占，不进入 Runtime WebSocket；不使用 EventSource、Socket.IO、reconnect、resume、replay 或 JSON success fallback。
- Runtime WebSocket 是 single owner，首轮 heartbeat causation、message union、queue capacity、close code 均严格验证；不自动 reconnect、drop、merge 或 replay。
- Manual SFTP bytes 只经 raw chunk endpoints，以 `application/octet-stream` 和 `X-Chunk-Offset` 关联；禁止 Base64 transport、path-embedded local path 和 high-level aggregate route。
- PTY input 只走 Runtime WebSocket；不得增加 generic RPC 或任意 command route。
- HTTP access log 只允许 method、route template、实际返回 status、duration 和 request ID；不得记录 raw path/query/header/body。Uvicorn native access log 关闭，避免生成另一份包含 client/raw URL 的访问日志。

## 凭据与存储风险

React 使用 Backend 公钥将用户输入包装为 RSA-OAEP-256 + AES-256-GCM request envelope，避免 secret 出现在 JSON plaintext transport body。envelope 只作为 `/v1/connections` 或 `/v1/agent/api-configs` 创建/更新请求的一部分提交，不提供独立 credential mutation route。Python handler 在同一 SQLite 事务内解封、写入 `CredentialRepository` 并创建或替换所属业务记录；业务删除也在同一事务内删除其拥有的凭据。Provider turn 与 SSH connection 从该仓库按 kind 解析短生命周期 secret buffer。

连接私钥选择与 strict UTF-8 读取由 React 拥有，HTTP 只携带加密 envelope，不携带文件路径。`GET /v1/runtime/credential-encryption-key` 仍是唯一公开的凭据辅助接口。

Runtime SQLite schema v6 是 plaintext store：credential secret、Agent conversation/message/output、remote recovery 及其他业务 payload 可能明文落盘。当前没有 at-rest encryption 或 OS-bound protection。旧 schema 明确拒绝且没有 migration；SQLite 不再保存无读取闭环的 Audit/Trace。日志、Problem、SSE terminal event、WebSocket event 和 UI store 均不得包含 secret、Provider body、tool/command、stdout/stderr 或文件 bytes；仅 provisional AI text 可在活动 tab 内存中短暂存在。

## Manual SFTP 权威

React 独占本地 picker、File handle、hash、chunk read/write 和同步 save decision；Python 看不到本地绝对路径。Python 独占 remote snapshot、temporary file、commit、abort 和 recovery record。页面 reload 会丢失本地 preparation，不承诺本地下载续传；remote recovery 不得自动联网或自动 mutation。

## 变更要求

任何 route、message、limit、credential envelope、ready/control frame、schema 或 ownership 变更必须同步实现、OpenAPI/fixture、两侧测试和文档。安全失败必须可见；禁止 fallback、兼容解析、猜测关联或静默降级。
