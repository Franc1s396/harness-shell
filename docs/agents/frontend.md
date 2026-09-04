# Frontend Guide

## 范围与职责

`frontend/src/` 是 React/TypeScript WebView。它负责展示、非敏感 UI 状态、typed loopback API、Runtime WebSocket，以及 Manual SFTP 的浏览器本地文件边界。改动 HTTP/WebSocket、安全或 Desktop 生命周期时，同时读取 Architecture、Protocol & Security 和 Rust Core Guide。

## 启动与 API 边界

- `src/api/bootstrap.ts` 是唯一 Tauri bootstrap 调用点；production 缺失或非法 Backend URL 必须显式失败。
- `src/api/http-client.ts` 独占 base URL、`X-Request-ID`、JSON/Problem 解析，以及 Agent `fetch()`/`ReadableStream` 的 strict SSE framing、UTF-8 与 65,536-byte frame/4,194,304-byte body budget。
- `src/api/runtime-websocket.ts` 独占 Runtime WebSocket、首轮 ping/pong、message validation 与 close handling。
- `src/api/agent.ts` 独占四类 Agent SSE event schema、连续 sequence、固定 request/conversation/run correlation 与 terminal-before-EOF 校验；不 reconnect、retry、resume 或 fallback。
- 各领域 `src/api/*.ts` 只封装固定 typed route；组件中不得散落 URL、裸 `fetch`、`invoke` 或 event listener。
- `features/connections/private-key-file.ts` 独占连接私钥文件选择、大小校验与 strict UTF-8 读取；只把短生命周期文本交给连接提交流程，不发送本地路径。
- `get_backend_bootstrap` 是唯一允许的自定义 Tauri command；不得新增业务 command 或独立 approval window。

## Manual SFTP 本地所有权

- `features/sftp/browser-file-gateway.ts` 独占 upload picker、同步 download save picker、File/System Access handle 和 262,144-byte chunk iteration。
- `browser-sha256.ts` 与 `browser-transfer-coordinator.ts` 在 React 侧执行本地 hash、二次一致性检查、raw upload/download chunk loop 与本地 write/close/abort。
- raw chunk 只能通过 `application/octet-stream`、`X-Chunk-Offset` 和固定 operation identity 传输；不得 Base64 包裹或发送本地绝对路径。
- local preparation 只在内存。reload、窗口关闭或失去 handle 后不能恢复本地 download；remote recovery 由 Python API 明确呈现。
- Manual SFTP 不提供 batch、drag/drop、recursive upload/download、directory merge 或 Agent 工具入口。

## 状态与交互

- Connection、Terminal、Agent 与 SFTP 只绑定用户显式选择的 connected Session；不得按列表顺序回退或在 tab 切换时偷换 owner。
- pending transfer/run 的 disconnect、Session close 和 application close 必须有显式门禁。
- unknown response/event、失联、stale identity/version 必须进入明确失败状态，不返回 success-shaped fallback。
- Agent Run 在首个 visible delta 前显示 thinking；delta 只进入 per-tab `activeRun.streamedText`，completed 后才写正式 assistant message。server failed、invalid、too-large 或 interrupted stream 必须清除 partial text并只显示 error；错误展示必须分别标出原始 `error_code` 与收到的 `error_message`，不得通过 i18n 替换异常信息。provisional 内容不显示 Run details，也不新增 Stop 控件。
- Agent 的 provisional 与 completed assistant text 使用 GitHub-flavored Markdown 展示；用户消息和错误保持纯文本。Markdown 渲染不得启用 raw HTML 或远程图片加载，外部链接必须使用隔离的新窗口属性，代码块和表格溢出只能在消息内容内部滚动。
- 凭据只以 Web Crypto 生成的 RSA-OAEP/AES-GCM request envelope，随所属 Connection 或 Provider mutation 发送；不存在独立 credential mutation endpoint，也不做 UI 补偿删除。secret 禁止写入 store、日志或错误详情。Backend 在同一业务事务中解封并以 schema-v6 plaintext credential record 保存，UI 必须把这一 at-rest 风险视为当前产品事实。

## 目录与测试

- `src/api/`：bootstrap、HTTP、WebSocket 和领域 client。
- `src/features/`：功能 UI、controller、纯逻辑与相邻测试。
- `src/stores/`：非敏感且 versioned 的 UI 偏好；不得保存 secret、runtime payload 或本地 handle。
- `src/i18n/`：`zh-CN`、`zh-TW`、`en` 资源真源。

最小验证：

```powershell
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
```

涉及 Desktop 行为时还要运行 Tauri tests 和适用仓库门禁；浏览器测试不等于真实 Tauri picker、窗口或安装版验收。
