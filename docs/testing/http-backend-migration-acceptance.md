# HTTP/WebSocket Backend Migration Acceptance

本记录只描述 2026-09-02 在当前 Windows checkout 上实际取得的证据。Python、Rust mock/contract、packaged backend、containerized OpenSSH、Tauri Desktop、真实 Provider、production SSH、authentication/TLS、remote deployment 和真实 migration 是彼此独立的证据层，任一 PASS 都不能替代其他层。

## 构建身份

- Checkout：`E:\codeSoftware\code\harness-shell`
- Branch：`develop`
- Base commit：`f5e63221b49bc463a68c0ab4fae5ded841affdf8`
- Worktree：dirty；保留用户已有工作与本次未提交实现，没有执行 stage、commit、push、branch 或 worktree 操作
- Windows：`Microsoft Windows NT 10.0.26200.0`
- Node / npm：`v22.13.1` / `10.9.2`
- Python：`3.12.13`
- Rust / Cargo：`1.98.0` / `1.98.0`
- Desktop 观察所用 Sidecar SHA-256：`0B9EF48883B4A23C2125873E56C0A62642FBD863ACB0CEE720264A61843A2AA0`

## 自动验证

| 层级 | 命令或门禁 | 结果 | 实际范围 |
| --- | --- | --- | --- |
| Python | `backend\.venv\Scripts\python.exe -m pytest backend -q` | PASS，`403 passed, 13 skipped` | Python domain、runtime、HTTP routes/models、WebSocket、存储与安全契约；13 项需要 SSH Lab 环境的用例在此独立运行中跳过 |
| Rust | `cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets` | PASS，exit code `0` | typed HTTP、Problem Details、single-owner WebSocket、READY gate、no reconnect/no respawn、3 秒 graceful timeout 后 Job kill、binary SFTP、Vault、Tauri command 与 packaged backend contract |
| Frontend | `npm.cmd --prefix frontend run test` | PASS，`52` files / `291` tests | React orchestration、退出门禁、Host Key、PTY、Manual SFTP、Agent Workspace 与 i18n；jsdom 的 canvas not-implemented 信息为已知 stderr，不影响退出码 |
| Frontend build | `npm.cmd --prefix frontend run build` | PASS | TypeScript 与 Vite production build；`139` modules transformed |
| Packaged backend | `packaged_backend_http_contract` 与 M3 packaged smoke | PASS | 当前 PyInstaller 构建的 Sidecar 完成 dynamic loopback port、initialize、ready、single WebSocket 和 shutdown contract；不等于安装包或部署验收 |
| HTTP artifacts | `backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --check` | PASS | OpenAPI、WebSocket schema、limits、Problem Details 与手写 fixture 无漂移 |
| Manual SFTP | `verify-manual-sftp.ps1`（由 M3 串联） | PASS | focused Python `75 passed`；real OpenSSH Direct/ProxyJump、binary upload/download、permission/cross-device fail-closed、PTY/SFTP channel isolation `4 passed` |
| M2 SSH Lab | `verify-m2.ps1`（由 M3 串联） | PASS | containerized OpenSSH 的 password/key/encrypted-key、Host Key change、ProxyJump 与相关 contracts；SSH integration `9 passed` |
| M3 Agent | `verify-m3-agent.ps1` | PASS | focused Agent/runtime/schema `140 passed`；fake ChatModels；bound-session Direct/ProxyJump、timeout、cancel `4 passed`；最终成功标志后容器与网络已清理 |

M3 最终成功标志：

```text
M3 Agent automated gate passed: local Windows checkout, fake ChatModels, packaged loopback backend, and containerized OpenSSH lab only.
```

## Tauri Desktop migration matrix

- 时间：2026-09-02 13:23 至 13:26、13:45 至 13:46（UTC+08:00）
- 启动：从 `frontend/` 执行 `npm run tauri:dev`；第二轮复用同一 packaged Sidecar，执行 `npm.cmd --prefix frontend run tauri -- dev`
- 观察方式：Windows 桌面窗口可访问性树与截图；没有向终端输入命令，没有连接现有 SSH 配置，没有接触或输入 credential

| 检查项 | 结果 | 观察 |
| --- | --- | --- |
| 应用与 Runtime 启动 | PASS | `Harness Shell` 主窗口完成渲染，底部状态明确显示 `Runtime: READY`；Connection、Terminal、Agent 区域均可见 |
| READY gate | PASS（桌面可见结果） | WebView 只在最终状态中观察到 `READY`；initialize、ready 和 WebSocket handshake 的顺序由 packaged/Rust contract 覆盖，未把内部帧暴露给 WebView |
| dynamic port 日志 | PASS | 第二轮日志记录 `Uvicorn running on http://127.0.0.1:59972` 与结构化 `http_server_listening`，host 为 `127.0.0.1`、port 为本轮动态端口 `59972` |
| Connection / Host Key / SSH / PTY | NOT RUN | 存在一个本地测试配置，但未获准连接；没有执行远程命令或 Host Key 决策 |
| Manual SFTP Desktop | NOT RUN | 没有 active connected terminal，因此没有执行 picker、binary transfer 或 mutation；仅有自动 contract 与 containerized OpenSSH 证据 |
| Agent Desktop | NOT RUN | 没有 connected terminal，也没有使用真实 Provider credential；界面正确提示先选择已连接终端 |
| WebSocket 断线后 FAILED 且不 reconnect | PASS | 精确终止本轮 PyInstaller server child PID `38660` 后，Rust 记录 `RUNTIME_WEBSOCKET_DISCONNECTED`；UI 显示 `Runtime: FAILED`、相同稳定 error code 并禁用交互；等待 10 秒只有主应用进程存在，没有 Sidecar respawn |
| active Agent/SFTP 退出门禁 | NOT RUN | 本轮没有 active Agent run 或 SFTP transfer；Frontend 自动测试覆盖对应门禁状态机 |
| 普通退出确认 | PASS | 关闭窗口后出现“退出 Harness Shell？”确认框，确认后应用退出 |
| 正常退出 child 清理 | PASS | 确认退出 5 秒后，`harness-shell.exe`、PyInstaller 两级 `harness-shell-sidecar.exe` 与本次 Vite/esbuild 进程均不存在 |
| grace timeout 后 Job kill | NOT RUN（Desktop fault injection） | 未故意制造 shutdown hang；3 秒 timeout 与 Job kill 仅由 Rust contract 覆盖 |

## 安全与能力边界

- 现行调用链是 `React -> Tauri command -> Rust RuntimeClientHandle/Vault -> loopback typed HTTP + Runtime WebSocket -> FastAPI/Uvicorn -> application/domain services`。
- Rust 独占 packaged child、dynamic port、runtime keys、credential resolution、single WebSocket 和进程树清理；WebView 不接触 base URL、port、runtime key、credential、原始 HTTP/WebSocket payload 或 Sidecar stderr。
- Python 只绑定 loopback，接受固定 typed routes；initialize key injection 只发生一次。当前不实现 HTTP authentication、TLS、remote bind、generic RPC、compatibility adapter、fallback transport、自动 reconnect 或自动 respawn。
- Manual SFTP chunk 使用 `application/octet-stream` 原始 bytes；失败不自动重放 mutation。

## 明确未验证

- Real Provider (`CHAT_COMPLETIONS` / `RESPONSES`)：NOT RUN。
- Production SSH host：NOT RUN；Docker OpenSSH Lab 不是生产主机。
- HTTP authentication：NOT IMPLEMENTED / NOT RUN。
- TLS：NOT IMPLEMENTED / NOT RUN。
- Remote deployment：NOT RUN；当前只允许 loopback。
- Installer/package deployment：NOT RUN；packaged Sidecar smoke 不等于桌面安装包验收。
- Existing-user real migration、rollback 与数据备份恢复：NOT RUN。
- Destructive production fault injection：NOT RUN；本轮只对本地 dev 实例执行了精确 PID 的 Sidecar disconnect injection。

## 结论

当前 checkout 的 typed loopback HTTP/WebSocket backend migration 已通过本地 Python、Rust、Frontend、packaged backend、contract artifact、containerized OpenSSH，以及 Tauri Desktop 的启动、动态端口、READY、WebSocket disconnect FAILED/no-respawn 与正常退出证据。真实 Provider、production SSH、authentication/TLS、remote deployment、安装包、真实 migration，以及 Desktop graceful-timeout/active-operation fault injection 仍未验收，不能由上述 PASS 推导为完成。
