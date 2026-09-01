# M3 Experimental React Shell Agent Frontend Acceptance

本记录只覆盖 2026-08-31 当前 checkout 的 React Agent 前端实现与实际执行证据。Frontend、M3 自动门禁、Tauri Desktop、真实 Provider、生产 SSH、部署和迁移是相互独立的证据层；任一层通过都不能推导其他层通过。

## 环境

- Tester: Codex local execution（用户已授权按计划实施与验证）
- Checkout: `E:\codeSoftware\code\harness-shell`
- Platform: Windows 11，PowerShell，Tauri 2/WebView2
- Git: 保留未提交工作区；未创建 worktree/branch，未 stage、commit 或 push
- Desktop language observed: `zh-TW`

## Frontend automated evidence

### Full Vitest suite

- Command: `cd frontend; npm.cmd test`
- Result: PASS，exit code `0`
- Exact count: `51` test files passed，`278` tests passed
- Covered Agent scope includes typed API command/payload mapping, versioned Provider preference sanitization, Provider create/update/delete compensation, per-tab reducer and stale completion rejection, secret form clearing, stacked Dialog behavior, Settings category transitions, non-streaming workspace, concurrent tab controller state, terminal badges, Session close/disconnect gate, application-close SFTP precedence and locale parity.
- Existing environment note: jsdom reports `HTMLCanvasElement.prototype.getContext` as not implemented while importing xterm color code; the suite still completed with exit code `0` and no failed test.

### Production frontend build

- Command: `cd frontend; npm.cmd run build`
- Result: PASS，exit code `0`
- TypeScript: PASS
- Vite: PASS，`138` modules transformed

## M3 local automated gate

- Command: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1`
- First sandboxed attempt: NOT A CODE RESULT；Docker config/named-pipe access was denied by the sandbox before the gate could start.
- Re-run outside the sandbox with the same command: PASS，exit code `0`
- Final marker: `M3 Agent automated gate passed: local Windows checkout, fake ChatModels, packaged Sidecar, and containerized OpenSSH lab only.`
- Focused Agent/runtime/schema Python phase: `136 passed`
- Bound-session OpenSSH Agent integration: `4 passed`
- Nested regression evidence: Manual SFTP automated gate PASS；Frontend `51` files / `278` tests PASS；production frontend build PASS.

该门禁使用 fake ChatModels，并验证 packaged Sidecar、Agent Protocol/Rust/Vault 边界和 containerized OpenSSH bound-session command 路径。它没有联系真实 Provider，也不证明完整 Desktop matrix 或生产 SSH。

## Tauri Desktop observations

- Command: `cd frontend; npm.cmd run tauri:dev`
- Result: PARTIAL PASS；Sidecar build、Vite、Rust dev build 与 `harness-shell.exe` 启动成功，结束验收后已停止 dev process。
- Observed window: `Harness Shell`，一次捕获尺寸约 `1442×864`；当前 Windows automation API 未能精确调整到计划要求的 `1280×720` 和 `900×600`，因此这两个指定尺寸均为 NOT RUN。

| Checklist | Result | Actual observation |
| --- | --- | --- |
| Settings/Provider dialog usability | PARTIAL PASS | 当前窗口下 Settings 与 Model Providers 空态完整可见；指定的 `1280×720`、`900×600` 未执行。 |
| Settings survives Runtime refresh | PASS for observed session | Runtime 保持 `READY`，Settings 在跨多个 1 秒刷新周期的交互过程中持续打开。 |
| Nested Escape and focus | PARTIAL PASS | Provider editor 打开后按 Escape 只关闭顶层 Dialog，外层 Settings 保持；视觉 focus ring 返回“新增 Provider”，Windows accessibility snapshot 将 focus 报告为 Web document，故精确 focus restoration 不提升为完整 PASS。 |
| API Key never reappears after failure/reopen | NOT RUN on Desktop | 未输入或持久化任何测试 secret。自动测试已证明 password input、stored reference 不渲染，以及 submit failure 后清空 replacement secret。 |
| Two connected tabs run concurrently | NOT RUN | 未提供隔离 Provider 与两个可用 SSH Session。 |
| Tab switch/collapse does not migrate/cancel Run | NOT RUN | 没有真实 Desktop Run；由 reducer/controller 自动测试覆盖。 |
| Active Run blocks close/disconnect before cleanup | NOT RUN | 没有真实 Desktop Run；由 TerminalWorkspace/WorkspaceController 自动测试与 first-line guard 覆盖。 |
| Agent-only Force exit and SFTP precedence | NOT RUN | 无法在 Desktop 中建立 active Run；WorkspaceFrame 自动测试覆盖精确按钮优先级。 |
| Keyboard, IME, screen-reader names, New conversation, 26×26 send | PARTIAL PASS | 可访问树识别 Settings、Model Providers、Provider fields 和 Agent 空态；Escape 已观察。IME、connected composer、New conversation 与 send button 未在 Desktop 执行。 |
| Agent empty state | PASS | 展开 Agent 后，在没有 active connected terminal tab 时显示明确空态，不伪造聊天或工具控制。 |

上述结果是局部 Tauri Desktop 观察，不是完整 Desktop acceptance。

## Provider、SSH 与生产边界

| Evidence layer | Result | Boundary |
| --- | --- | --- |
| `CHAT_COMPLETIONS` Provider | NOT RUN | 用户未提供测试 Provider credential。 |
| `RESPONSES` Provider | NOT RUN | 用户未提供测试 Provider credential。 |
| Isolated real SSH target for Agent UI | NOT RUN | 仅自动门禁使用 containerized OpenSSH Lab。 |
| Production SSH host | NOT RUN | 未连接生产主机。 |
| Deployment/migration | NOT RUN | build、schema tests 和 packaged initialize 不等同部署或迁移验收。 |
| Approval/sudo/automatic recovery | NOT IMPLEMENTED OR NOT ACCEPTED | React UI 未发明这些后端能力。 |

## No-secret evidence

- React 持久化 Store 只允许 `preferredApiConfigId` UUID；测试注入的额外 `apiKey` 字段会被 sanitize 丢弃。
- Provider API Key 使用受控 `type="password"`、`autoComplete="new-password"` 输入；Dialog 关闭、重新打开以及 submit 校验失败、命令成功或命令失败后都会清空内存值。
- 编辑表单只显示“API Key safely stored”状态，不渲染 `api_key_secret_ref`；typed API 只把 secret 发送给 `store_model_api_key`，配置 CRUD 使用 credential reference。
- 本次 Desktop 观察没有输入、显示、记录或传输任何真实 API Key。

## Known limitations

- Agent turn 是非流式单 Promise；没有 Stop、streaming、工具参数/原始 stdout 展示或任意 shell 输入。
- Provider 与 Session 在每次 turn 开始时冻结；正在运行的配置不可编辑/删除，Session close/disconnect 会在 cleanup 前被拒绝。
- 应用 Force exit 不发送不存在的 Agent cancel command，退出后的 remote outcome 可能未知。
- 本记录没有完成两个指定窗口尺寸、IME、两个真实并发 tab、真实 Provider、生产 SSH、部署或迁移验收。

## Result

- React Agent frontend implementation: PASS for full automated suite and production build.
- M3 local automated gate: PASS for its exact fake/packaged/container scope.
- Tauri Desktop Agent acceptance: PARTIAL PASS only for the observations above.
- Real Provider, production SSH, deployment and migration acceptance: NOT RUN.
