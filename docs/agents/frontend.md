# Frontend Guide

## 范围与职责

`frontend/src/` 是 React/TypeScript WebView。它负责展示、非敏感 UI 状态、typed loopback API、Runtime WebSocket，以及 Manual SFTP 的浏览器本地文件边界。改动 HTTP/WebSocket、安全或 Desktop 生命周期时，同时读取 Architecture、Protocol & Security 和 Rust Core Guide。

## 启动与 API 边界

- `src/api/bootstrap.ts` 是唯一 Tauri bootstrap 调用点；production 缺失或非法 Backend URL 必须显式失败。
- `src/api/http-client.ts` 独占 base URL、`X-Request-ID`、JSON/Problem 解析，以及 Agent `fetch()`/`ReadableStream` 的 strict SSE framing、UTF-8 与 65,536-byte frame/4,194,304-byte body budget。
- `src/api/runtime-websocket.ts` 独占 Runtime WebSocket、首轮 ping/pong、message validation 与 close handling。
- `src/api/agent.ts` 独占八类 Agent SSE event schema、连续 sequence、固定 request/conversation/run correlation 与 terminal-before-EOF 校验；不 reconnect、retry、resume 或 fallback。
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

- 原生滚动区域统一使用 `src/styles/globals.css` 的 WebKit 滚动条样式，沿用 Agent 的透明轨道、8px 宽/高及圆角滑块；不再依赖 Agent 专用 class，也不混用会覆盖 WebView2 伪元素样式的 `scrollbar-color`/`scrollbar-width`。
- Connection、Terminal、Agent 与 SFTP 只绑定用户显式选择的 connected Session；不得按列表顺序回退或在 tab 切换时偷换 owner。
- pending transfer/run 的 disconnect、Session close 和 application close 必须有显式门禁。
- unknown response/event、失联、stale identity/version 必须进入明确失败状态，不返回 success-shaped fallback。
- Agent 收到 `tool_started` 后保留文本并显示“工具执行中…”，直到下一合法业务事件；按 turn 顺序累计工具名、调用 ID 与完整参数，完成后随 assistant 消息保存于页面内存，在回答下方提供默认折叠的“执行工具（数量）”，参数以纯文本 JSON 展示，无调用则不显示。失败、取消或断流丢弃临时列表。连续工具事件保持提示，文本更新清除提示，完成、失败、取消或断流清除活动状态。
- Agent Run 在首个 visible delta 前显示 thinking（工具执行提示优先）；RUNNING 且非等待审核时显示加载图标，已有 provisional 文本时图标位于回复下方，完成或失败后移除；delta 追加、text_replace 完整替换（允许清空）per-tab `activeRun.streamedText`，completed 后才写正式 assistant message。server failed、invalid、too-large 或 interrupted stream 必须清除 partial text并只显示 error；错误展示必须分别标出原始 `error_code` 与收到的 `error_message`，不得通过 i18n 替换异常信息。provisional 内容不显示 Run details。RUNNING 时原发送按钮原位切换为取消回答（方形图标），通过每 tab 独立的 AbortController 中止本轮 POST SSE；等待首事件时也可取消。请求退出后清除 partial text、保留用户消息和历史，并显示普通“已取消”提示，恢复发送。保留已知 conversation ID；本地取消不伪造服务端 Run 终态，已校验终态优先于随后取消，网络和协议失败不得伪装成取消。取消不能撤销已执行命令，也不保证远程进程停止。
- Agent 的 provisional 与 completed assistant text 使用 GitHub-flavored Markdown 展示；用户消息和错误保持纯文本。Markdown 渲染不得启用 raw HTML 或远程图片加载，外部链接必须使用隔离的新窗口属性，代码块和表格溢出只能在消息内容内部滚动。
- 凭据只以 Web Crypto 生成的 RSA-OAEP/AES-GCM request envelope，随所属 Connection 或 Provider mutation 发送；不存在独立 credential mutation endpoint，也不做 UI 补偿删除。secret 禁止写入 store、日志或错误详情。Backend 在同一业务事务中解封并以 plaintext credential record 保存，UI 必须把这一 at-rest 风险视为当前产品事实。

Provider 新建和编辑弹窗将 context window size、压缩百分比和 max output tokens 收入默认折叠的“高级设置”，每次打开弹窗重置为折叠；展开后可修改。新建默认值为 128000、75%、8192，编辑及折叠保存时保留原预算，预算校验失败自动展开显示错误。draft 保持字符串，校验正安全整数、非空有限百分比与输入/输出的交叉预算，再将百分比除以 100 发送。保存失败保留预算 draft 并清空 API Key。摘要不进入聊天 state，压缩期间继续 thinking，历史消息内容和顺序不变，仅主回答或安全错误进入展示。

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


## Agent 审核气泡

`AgentApprovalBubble` 展示冻结目标、原始命令与拒绝/通过按钮，所有字段纯文本，内容溢出局限在气泡内部。`agent-state` 将审核作为独立记录保留，text_replace、completed、取消不能覆盖审核历史；记录不在消息流中独立渲染。待审核操作固定在发送栏上方，与输入区共用圆角外框，以单条分隔线连接，与消息滚动区域分离。固定审核区仅命令内容限制高度并内部滚动，顶部说明、目标及底部按钮不随命令滚动；审核区外层不设置滚动容器。提交时立即收起，可重试错误恢复操作气泡，UNKNOWN/INVALIDATED 只显示状态与错误，不提供操作按钮。终态消息绑定本 Run 的审核 ID，最终回答、错误或取消提示下方提供默认折叠的审核记录，按发生顺序展示目标、命令和结果；历史没有操作按钮，晚到的决定仍更新原记录。等待不显示倒计时或忙碌 spinner，后台 tab/侧栏使用 AWAITING_APPROVAL；原发送按钮仍可取消。

`useAgentController.decideApproval` 冻结 tab/requestToken/Run 身份，防重复点击，提交时两按钮禁用。决定成功只表示已记录；执行和继续推理由原 SSE 发布。取消、断连和旧响应不得修改新 Run；网络结果未知禁用审核并标记 UNKNOWN，确定校验/容量错误允许手动重试，没有自动重试。审核及命令不写持久化 UI store。首次发送风险弹窗和实验性 Agent 黄色警告框已删除，Provider 配置/重置对话等其他门禁保留。
