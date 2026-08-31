# Frontend Guide

## 何时必须读取

修改以下内容时必须读取本文档：

- React 组件、页面、Dialog、Workspace 或 Terminal UI；
- TypeScript 业务状态、Zustand Store、i18n 或 CSS/Tailwind；
- `frontend/src/api/` 中的 Tauri command/event wrapper；
- 前端单元测试、交互测试、生产构建或桌面 UI 验收。

若改动涉及 Tauri command、权限、凭据、Protocol 或 Sidecar 生命周期，还必须读取 [Rust Core Guide](rust-core.md) 和 [Protocol & Security Guide](protocol-security.md)。

## 范围与职责

Frontend 是 React/TypeScript WebView 展示层，负责：

- 呈现 Connection Navigator、Terminal Workspace、Agent 占位区、Context Bar 和 Status Bar；
- 收集用户输入并调用 typed Tauri API；
- 保存经过白名单约束的非敏感 UI 偏好；
- 将 Rust Core 投影的安全状态转换为明确的 UI 状态；
- 支持 `zh-CN`、`zh-TW` 和 `en` 界面资源。

Frontend 不拥有凭据、原始 Host Key 数据、SSH/PTY runtime object、Sidecar 状态机或 Protocol framing。

## 当前源码真源

- 应用入口：[frontend/src/App.tsx](../../frontend/src/App.tsx)、[frontend/src/main.tsx](../../frontend/src/main.tsx)
- 工作区编排：[frontend/src/features/workspace/WorkspaceController.tsx](../../frontend/src/features/workspace/WorkspaceController.tsx)
- Tauri API wrapper：[frontend/src/api/](../../frontend/src/api/)
- 通用 UI primitive：[frontend/src/components/ui/](../../frontend/src/components/ui/)
- Feature 模块：[frontend/src/features/](../../frontend/src/features/)
- Zustand Store：[frontend/src/stores/](../../frontend/src/stores/)
- i18n：[frontend/src/i18n/](../../frontend/src/i18n/)
- 全局样式：[frontend/src/styles/globals.css](../../frontend/src/styles/globals.css)
- 脚本与依赖：[frontend/package.json](../../frontend/package.json)

## 项目结构规范

- `src/components/ui/`：可复用展示 primitive；不得拥有 SSH、凭据或持久化业务权威。
- `src/features/<feature>/`：功能 UI、局部控制器、纯逻辑和相邻测试；跨 feature 依赖需经过明确公共接口。
- `src/api/`：WebView 调用 Tauri command 和监听允许事件的唯一封装层；组件中不得散落裸 `invoke`、事件名或 payload 拼装。
- `src/stores/`：非敏感 UI 状态。任何持久化 Store 必须 versioned、allowlisted，并对未知版本 fail closed 到明确默认值。
- `src/i18n/`：语言解析和资源真源；界面 copy 通过翻译 key 获取。
- `src/styles/`：Tailwind CSS 4、全局 token、xterm 样式和共享视觉规则。
- `src/approval/`：固定审批窗口入口；当前 M2 仅保留诚实边界，不得伪造 M3 审批流程。

新增组件应放在最接近其业务所有权的位置。只有被多个 feature 稳定复用、且不携带业务权威的组件才进入 `components/ui`。

## 代码规范

- 使用严格 TypeScript；组件 props、Hook 输入输出、Store action 和 API payload 保持显式类型。
- 组件负责展示与交互编排；可独立验证的状态转换、格式转换和生命周期逻辑提取为纯函数或 focused Hook。
- Tauri 调用统一经过 `src/api/`；API wrapper 将 command 错误保留为结构化失败，不返回 success-shaped fallback。
- 用户手动 SFTP 的 21 个 wrappers、严格 progress event validator 和安全错误归一化位于 `src/api/manual-sftp.ts`；controller 进入 SFTP Activity 时只绑定 `sessions.find(tabId === activeTabId)` 得到的显式已连接 Session，不按列表顺序回退，也不因之后切换 active tab 隐式迁移。离开再进入才重新选择绑定。
- manual SFTP listing、selection、preparation、progress、recovery 和每个 Session 的 `lastPath` 只存在于 controller/reducer 内存，不进入 Zustand persist 或 localStorage。切换目录先关闭旧 listing；完整 listing 不再发送 close，未完成 cursor 才显式关闭；stale listing identity/path/sequence 直接拒绝。
- `features/sftp/ManualSftpWorkspace.tsx` 是用户手动文件管理 UI 真源：提供单选 lazy remote tree/table、path/up、显式上传/下载/新建、file/directory/symlink 的 Rename/Move/Delete/Properties 与 regular-file SHA-256、transfer strip 和 Recovery Center。Move 复用 rename；不提供 batch、drag/drop、directory merge 或 recursive upload/download。`cleanup_required`/`outcome_unknown` 只自动打开恢复界面和读取本地恢复记录，不自动联网或执行 mutation；`outcome_unknown` UI 只允许 Verify。900×600 时只隐藏 remote tree，不使用缩放变换。
- Remote entry type 与 transfer phase 只显示本地化文案，不能把 wire enum 原值直接渲染给用户；符号链接的 Properties/Read link target 通过同一个 typed inspect command 先 lstat、再显式 readlink。
- SFTP grid 键盘契约为 ArrowUp/ArrowDown、Enter、Backspace、F2、Delete 与 Ctrl+R；native Open/Save As 由 Rust command 打开，本地 path 不进入 TypeScript。SFTP 状态只在 workspace/controller 内存中，persist schema v3 只新增 `activeActivity="sftp"` 这一非敏感 UI 偏好。
- 用户请求 Disconnect 或 application close 时，Frontend 必须显示 Continue waiting / Cancel and clean up；committing 只能等待，cleanup 失败后才可显式 Keep recovery record and close。决策前不得先断连、取消或关闭。
- `useEffect` 必须有清晰依赖和 cleanup；event listener、timer、xterm instance、ResizeObserver 等资源必须确定性释放。
- Zustand 只保存前端需要的非敏感状态。持久化使用显式 `version`、`partialize`、migration 和 sanitize。
- 不得持久化凭据、PTY output、Host Key candidate、approval payload、runtime error detail、live connection/session ID 或远程命令内容。
- 新增 UI 文案同步维护 `zh-CN`、`zh-TW`、`en`；协议名、命令、远程输出、error code 和不可翻译标识保持原文。
- 新增或修改行为时优先在相邻位置维护 `*.test.ts` 或 `*.test.tsx`；测试用户可见行为和契约，不绑定无关 DOM 细节。
- 复杂交互注释说明焦点、关闭顺序、竞态和资源生命周期原因，不逐行翻译 JSX。

## 长期约束

- WebView 只能获得 Rust Core 明确投影的安全数据，不得访问 Vault、raw frame、stderr 或任意 shell。
- `WorkspaceController` 负责编排当前 WebView 工作流，但不成为 SSH、PTY 或凭据的第二权威。
- Runtime、Connection 和 Terminal 的权威状态来自 Rust/Sidecar 返回或允许事件；UI optimistic state 不得覆盖失败事实。
- `ConnectionProfile.version` 是 Sidecar 返回的 JS-safe 正整数快照，Frontend 只按 typed API 保留和展示连接数据；凭据解析后的陈旧检查由 Rust 传递数字 `profile_version`、Sidecar 在网络 I/O 前执行，不能用 `updated_at` 替代。
- UI 持久化只允许稳定偏好。当前持久化白名单和版本逻辑以 `workspace-ui-store.ts`、`locale-store.ts` 及其测试为真源。
- 主界面保持 terminal-first；实验性 M3 Agent 后端虽已存在，但 Agent Workspace 尚未接入，必须继续呈现诚实的 unavailable/placeholder 状态，不提供虚假聊天或工具控制。
- 布局、i18n 和交互的自动测试不能替代 Tauri 桌面实际窗口、焦点和 Runtime 刷新验收。

## 项目命令

以下命令从 `frontend/` 运行：

```powershell
npm install
npm run dev
npm run test
npm run build
npm run tauri:dev
npm run tauri:build
npm run tauri info
```

- `npm run test`：执行 Vitest。
- `npm run build`：执行 TypeScript 检查和 Vite 多页生产构建。
- `npm run tauri:dev`、`npm run tauri:build`：先构建 Python Sidecar，再启动或打包 Tauri。
- `npm run tauri info`：检查本机 Tauri、Rust、WebView2 和 Windows 工具链状态。

## 验证要求

- 纯函数、Store 或 Hook：运行对应 test file，再运行 `npm run test`。
- React 交互：覆盖正常、失败、取消/关闭、键盘和焦点路径；相关时检查未知持久化版本。
- API wrapper：验证 command 名、payload shape、返回类型和结构化错误。
- UI 结构或样式：运行 `npm run test` 和 `npm run build`；涉及真实窗口、xterm、焦点或 Runtime 刷新时执行 Tauri 桌面验收。
- 跨 Rust/Sidecar 变更：除前端测试外，运行 [Testing Guide](testing.md) 指定的相关 Rust/Python/门禁验证。

## 何时需要更新本文档

以下变化必须同步更新本文档：

- `frontend/src/` 目录职责、入口或主要 feature 边界改变；
- 新增状态库、持久化字段、语言或构建命令；
- Tauri API wrapper、事件订阅或 WebView 可见数据边界改变；
- terminal-first 布局或 M3 Agent Workspace 的真实实现状态改变；
- 前端测试或桌面验收入口改变。

纯视觉微调或组件内部重构未改变长期规则时无需更新，但必须完成任务末尾 AGENTS 影响检查。
