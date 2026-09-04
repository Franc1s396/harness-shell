const en = {
  common: {
    appName: "Harness Shell", cancel: "Cancel", save: "Save",
    saveAndConnect: "Save & Connect", delete: "Delete", close: "Close",
    copyDetails: "Copy details", copied: "Copied",
  },
  nav: {
    connections: "SSH Connections", files: "Files", sftp: "SFTP", settings: "Settings",
    agent: "AI Agent", milestone: "Available in {{milestone}}",
  },
  shell: {
    primaryActivities: "Primary activities",
    resizeSidebar: "Resize connection sidebar",
    resizeAgent: "Resize Agent workspace",
  },
  applicationClose: {
    title: "Exit Harness Shell?",
    body: "Closing the application will end the current workspace and all sessions.",
    confirm: "Exit Harness Shell",
    activeTransferBody: "A manual SFTP transfer is still active. Wait for it to finish or cancel it and wait for cleanup before exiting.",
    committingBody: "A manual SFTP transfer is committing and can no longer be cancelled safely. Continue waiting for it to finish.",
    recoveryBody: "Transfer cleanup could not be completed. The protected recovery record will be kept for the next launch.",
    continueWaiting: "Continue waiting",
    cancelAndCleanUp: "Cancel and clean up",
    keepRecoveryAndClose: "Keep recovery record and close",
    activeAgentBody: "{{count}} Agent Run(s) are still active. Remote outcome may be unknown after exit.",
    forceExit: "Force exit",
  },
  topbar: {
    noConnection: "No connection selected", toggleSidebar: "Toggle connection sidebar",
    language: "Language", followSystem: "Follow system", terminal: "Focus terminal",
    quickActions: "Quick actions", newConnection: "New connection", editConnection: "Edit connection",
    focusTerminal: "Focus active terminal", localEnvironment: "Local",
  },
  activity: { settings: "Settings", filesUnavailable: "Files are planned for M3" },
  connections: {
    title: "Connections", search: "Search host, name, or group", new: "New connection",
    actions: "Connection actions", open: "Open connection",
    edit: "Edit connection", connect: "Connect", disconnect: "Disconnect", reconnect: "Reconnect",
    noResults: "No connections found", ungrouped: "Ungrouped", favorite: "Favorite",
    basic: "Basic", authentication: "Authentication", advanced: "Advanced",
    displayName: "Connection name", group: "Group", host: "Host", port: "Port",
    username: "Username", authMethod: "Authentication method", password: "Password",
    privateKey: "Private key", passphrase: "Passphrase", proxyJump: "ProxyJump",
    direct: "Direct connection", importKey: "Import private key…", keySelected: "Private key selected",
    required: "Required", invalid: "Invalid value", keepCurrent: "Leave blank to keep the current secret",
    hostKeyStatus: "Host Key", deleteConfirmTitle: "Delete connection?",
    deleteConfirmBody: "This removes the profile, not the remote host.", confirmDelete: "Confirm delete",
    savedConnectFailed: "The profile was saved, but the connection failed.",
  },
  terminal: {
    title: "Interactive terminals", runtimeReady: "Runtime ready", inputDisabled: "Input disabled",
    emptyTitle: "No terminal is open", emptyBody: "Select a connection or create a new profile.",
    selectConnection: "Select connection", createConnection: "New connection", closeTab: "Close {{name}}",
    humanBoundary: "Human PTY — Agent input is isolated",
    sidecarUnavailable: "Sidecar unavailable. Existing PTYs are disconnected; no fallback terminal was created.",
    actions: "Session actions", reconnect: "Reconnect", disconnect: "Disconnect",
    closeConfirmTitle: "Close session?",
    closeConnectedBody: "The tab will close immediately. Harness Shell will close its PTY and disconnect SSH in the background.",
    closeLocalBody: "This removes the local tab and retained terminal output.",
    confirmClose: "Close session", reconnectDivider: "── Reconnected ──",
    cleanupFailed: "Could not finish cleaning up {{name}}",
    retryCleanup: "Retry cleanup", cleanupRetrying: "Retrying cleanup…",
    states: {
      connecting: "Connecting", hostKeyRequired: "Waiting for Host Key",
      connected: "Connected", disconnecting: "Disconnecting",
      disconnected: "Disconnected", failed: "Connection failed",
    },
  },
  sftp: {
    title: "Remote files", noSessionTitle: "No connected terminal selected", noSessionBody: "Open or select a connected terminal tab before using SFTP.", selectConnection: "Select connection",
    loadingRemoteFiles: "Loading remote files…", loadingDirectory: "Loading {{path}}…", loadingTreeDirectory: "Loading directories in {{path}}…", loadingProperties: "Loading properties for {{name}}…", calculatingHash: "Calculating SHA-256 for {{name}}…", resolvingLink: "Resolving {{name}}…", loadingRecoveries: "Loading recovery records…",
    path: "Remote path", go: "Go", parent: "Parent directory", refresh: "Refresh", upload: "Upload file", download: "Download", newFolder: "New folder", rename: "Rename", delete: "Delete",
    name: "Name", size: "Size", type: "Type", modified: "Modified", empty: "This directory is empty.", tree: "Remote directory tree", actions: "Actions", open: "Open", openTarget: "Open target", move: "Move", properties: "Properties", readLinkTarget: "Read link target",
    uploadNameTitle: "Upload file", targetName: "Remote file name", continue: "Continue", transferTitle: "Confirm {{direction}}?", confirmTransfer: "Start transfer", confirmRenameOverwrite: "Confirm overwrite",
    overwriteWarning: "The destination already exists and will be atomically replaced.", externalRace: "No remote lock is held. An external client's same-path update during the commit window can be replaced by the final atomic rename.",
    mkdirTitle: "Create directory", renameTitle: "Rename {{name}}", moveTitle: "Move {{name}}", propertiesTitle: "Properties — {{name}}", newName: "New name", targetPath: "Target path", mode: "Mode", linkTarget: "Link target", deleteTitle: "Delete {{name}}?", deleteBody: "This action operates on the selected remote entry only.", confirmDelete: "Confirm delete",
    recursiveTitle: "Confirm recursive delete", recursiveSummary: "{{files}} files, {{directories}} directories, {{links}} symlinks, {{bytes}} bytes under {{path}}.", confirmRecursive: "Delete recursively",
    transfer: "Transfer", cancelOperation: "Cancel operation", recoveries: "Recovery center", noRecoveries: "No recovery work is pending.", verify: "Verify result", executeRecovery: "Run recovery", viaSymlink: "via symlink",
    disconnectTransferTitle: "Active SFTP transfer", disconnectTransferBody: "This SSH session still owns a manual SFTP transfer. Continue waiting or cancel it and wait for cleanup before disconnecting.", disconnectCommittingBody: "The transfer is committing and can no longer be cancelled safely. Continue waiting.", disconnectRecoveryBody: "Cleanup could not be completed. Keep the protected recovery record before disconnecting.", keepRecoveryAndDisconnect: "Keep recovery record and disconnect",
    recoveryConfirmTitle: "Confirm recovery action", recoveryConfirmBody: "{{action}} changes retained recovery state and requires explicit confirmation.", confirmRecoveryAction: "Confirm recovery action",
    recoveryStates: { cleanup_required: "Cleanup required", outcome_unknown: "Outcome unknown", recovery_required: "Recovery required" },
    recoveryActions: { deleteTemp: "Delete temporary file", continueDelete: "Continue delete", restoreTombstone: "Restore tombstone", keep: "Keep for later" },
    entryTypes: { file: "File", directory: "Directory", symlink: "Symbolic link", other: "Other" },
    transferPhases: { preparing: "Preparing", transferring: "Transferring", verifying: "Verifying", committing: "Committing" },
  },
  agent: {
    title: "Agent", unavailable: "Agent is not enabled in M2", expand: "Expand Agent information", collapse: "Collapse Agent information", boundary: "The Agent channel remains isolated from this human PTY.", unavailableTitle: "Agent arrives in M3", unavailableBody: "This M2 workspace does not execute Agent tasks.",
    message: "Message", messagePlaceholder: "Ask Agent to inspect or operate on this SSH session…", provider: "Provider", chooseProvider: "Choose Provider", providerSettings: "Provider settings", openProviderSettings: "Open Provider settings", send: "Send message", enterToSend: "Enter to send · Shift+Enter for newline", running: "Receiving response…", thinking: "Thinking…", newConversation: "New conversation", resetTitle: "Start a new conversation?", resetBody: "This clears only the current tab's in-memory conversation view.", confirmReset: "Start new conversation", riskTitle: "Allow experimental remote command execution?", riskBody: "Agent can execute shell commands in this SSH Session. Commands may modify the remote host; there is no per-command approval, reliable frontend stop, or production safety guarantee.", confirmRisk: "Acknowledge and send", compactRisk: "Experimental Agent: commands may modify the remote host and cannot be reliably stopped from this UI.", emptySession: "Select a connected terminal tab to use Agent.", emptyProvider: "Configure and select an enabled Provider before sending.", runDetails: "Run details", sentSnapshot: "Sent snapshot", runId: "Run ID", runStatus: "Status", iteration: "Iteration", session: "Session", apiType: "API type", model: "Model", noMessages: "No messages in this tab.", completedAnnouncement: "Agent completed for {{name}}", failedAnnouncement: "Agent failed for {{name}}", tabRunning: "Agent running for {{name}}", tabCompleted: "Agent completed for {{name}}", tabFailed: "Agent failed for {{name}}", activeRunTitle: "Agent Run is still active", activeRunBody: "{{name}} still owns an active Agent Run.",
  },
  settings: { language: "Language", general: "General", close: "Close settings", diagnostics: { title: "Diagnostics", description: "Harness Shell writes local application and Agent logs here.", loading: "Checking log directory…", available: "The log directory is available.", open: "Open log directory", opening: "Opening…" }, modelProviders: { title: "Model Providers", newProvider: "New Provider", empty: "No model providers configured.", loading: "Loading providers…", retry: "Retry", enabled: "Enabled", disabled: "Disabled", storedKey: "API Key safely stored", activeRun: "In use by an active Run", createTitle: "New Provider", editTitle: "Edit Provider", deleteTitle: "Delete Provider?", deleteBody: "Delete {{name}} and its stored API Key?", confirmDelete: "Confirm delete", displayName: "Display name", apiType: "API type", baseUrl: "Base URL", model: "Model", apiKey: "API Key", keepCurrentKey: "Leave blank to keep the current API Key", required: "Required", invalid: "Invalid value", save: "Save", primaryFailure: "Provider operation failed" } },
  language: { system: "Follow system", zhCN: "简体中文", zhTW: "繁體中文", en: "English" },
  status: { runtime: "Runtime", ssh: "SSH", hostKey: "Host Key", pty: "PTY size", agent: "Agent", route: "Route" },
  hostKey: {
    identity: "Host identity", changed: "Host Key changed", trust: "Trust this host?",
    changedBody: "Verify both fingerprints through a separate channel before replacing the trusted key.",
    trustBody: "Verify this fingerprint through a trusted channel before connecting.",
    algorithm: "Algorithm", trustedFingerprint: "Trusted fingerprint", newFingerprint: "New fingerprint",
    fingerprint: "SHA-256 fingerprint", replace: "Replace trusted key", trustConnect: "Trust and connect",
  },
  errors: {
    sshFailed: "SSH operation failed", hostKeyConflict: "Trusted Host Key changed again",
    profileSavedConnectFailed: "Profile saved; connection failed", technicalDetails: "Technical details",
    whatNext: "Review the details and retry only after correcting the reported cause.",
    retry: "Retry", edit: "Edit connection",
  },
  runtime: { failedTitle: "Runtime unavailable", failedBody: "Interactive actions are disabled while the local Runtime is unavailable.", retryStatus: "Check runtime again" },
};

const zhCN = {
  common: { appName: "Harness Shell", cancel: "取消", save: "保存", saveAndConnect: "保存并连接", delete: "删除", close: "关闭", copyDetails: "复制详情", copied: "已复制" },
  nav: { connections: "SSH 连接", files: "文件", sftp: "SFTP", settings: "设置", agent: "AI Agent", milestone: "将在 {{milestone}} 提供" },
  shell: { primaryActivities: "主要活动", resizeSidebar: "调整连接侧栏宽度", resizeAgent: "调整 Agent 工作区宽度" },
  applicationClose: { title: "退出 Harness Shell？", body: "关闭程序将结束当前工作区和所有会话。", confirm: "退出 Harness Shell", activeTransferBody: "仍有用户手动 SFTP 传输正在进行。请继续等待，或取消传输并等待清理完成后再退出。", committingBody: "用户手动 SFTP 传输正在提交，已不能安全取消。请继续等待传输完成。", recoveryBody: "传输清理未能完成。受保护的恢复记录会保留到下次启动。", continueWaiting: "继续等待", cancelAndCleanUp: "取消并清理", keepRecoveryAndClose: "保留恢复记录并关闭", activeAgentBody: "仍有 {{count}} 个 Agent Run 正在运行。退出后远程结果可能未知。", forceExit: "强制退出" },
  topbar: { noConnection: "未选择连接", toggleSidebar: "切换连接侧栏", language: "语言", followSystem: "跟随系统", terminal: "聚焦终端", quickActions: "快捷操作", newConnection: "新建连接", editConnection: "编辑连接", focusTerminal: "聚焦活动终端", localEnvironment: "本地" },
  activity: { settings: "设置", filesUnavailable: "文件功能计划在 M3 提供" },
  connections: { title: "连接", search: "搜索主机、名称或分组", new: "新建连接", actions: "连接操作", open: "打开连接", edit: "编辑连接", connect: "连接", disconnect: "断开", reconnect: "重新连接", noResults: "没有匹配的连接", ungrouped: "未分组", favorite: "收藏", basic: "基础信息", authentication: "身份认证", advanced: "高级设置", displayName: "连接名称", group: "分组", host: "主机", port: "端口", username: "用户名", authMethod: "认证方式", password: "密码", privateKey: "私钥", passphrase: "私钥口令", proxyJump: "ProxyJump", direct: "直连", importKey: "导入私钥…", keySelected: "已选择私钥", required: "必填", invalid: "值无效", keepCurrent: "留空以保留现有秘密", hostKeyStatus: "Host Key", deleteConfirmTitle: "删除此连接？", deleteConfirmBody: "只删除本地连接配置，不影响远端主机。", confirmDelete: "确认删除", savedConnectFailed: "配置已保存，但连接失败。" },
  terminal: { title: "交互式终端", runtimeReady: "Runtime ready", inputDisabled: "输入已禁用", emptyTitle: "尚未打开终端", emptyBody: "请选择连接或新建连接配置。", selectConnection: "选择连接", createConnection: "新建连接", closeTab: "关闭 {{name}}", humanBoundary: "人工 PTY — 与 Agent 输入隔离", sidecarUnavailable: "Sidecar 不可用。现有 PTY 已断开；未创建兜底终端。", actions: "会话操作", reconnect: "重新连接", disconnect: "断开连接", closeConfirmTitle: "关闭会话？", closeConnectedBody: "标签会立即关闭。Harness Shell 将在后台关闭 PTY 并断开 SSH。", closeLocalBody: "这将移除本地标签和保留的终端输出。", confirmClose: "关闭会话", reconnectDivider: "── 已重新连接 ──", cleanupFailed: "未能完成 {{name}} 的清理", retryCleanup: "重试清理", cleanupRetrying: "正在重试清理…", states: { connecting: "正在连接", hostKeyRequired: "等待 Host Key", connected: "已连接", disconnecting: "正在断开", disconnected: "已断开", failed: "连接失败" } },
  sftp: { title: "远程文件", noSessionTitle: "未选择已连接的终端", noSessionBody: "使用 SFTP 前，请打开或选择一个已连接的终端标签。", selectConnection: "选择连接", loadingRemoteFiles: "正在加载远程文件…", loadingDirectory: "正在加载 {{path}}…", loadingTreeDirectory: "正在加载 {{path}} 中的目录…", loadingProperties: "正在读取 {{name}} 的属性…", calculatingHash: "正在计算 {{name}} 的 SHA-256…", resolvingLink: "正在解析 {{name}}…", loadingRecoveries: "正在加载恢复记录…", path: "远程路径", go: "前往", parent: "上级目录", refresh: "刷新", upload: "上传文件", download: "下载", newFolder: "新建文件夹", rename: "重命名", move: "移动", properties: "属性", open: "打开", openTarget: "打开目标", readLinkTarget: "读取链接目标", delete: "删除", name: "名称", size: "大小", type: "类型", modified: "修改时间", actions: "操作", empty: "此目录为空。", tree: "远程目录树", uploadNameTitle: "上传文件", targetName: "远程文件名", targetPath: "目标路径", continue: "继续", transferTitle: "确认{{direction}}？", confirmTransfer: "开始传输", confirmRenameOverwrite: "确认覆盖", overwriteWarning: "目标已存在，将使用原子操作替换。", externalRace: "远程端未加锁。提交窗口内，其他客户端对同一路径的更新可能被最终原子重命名替换。", mkdirTitle: "新建目录", renameTitle: "重命名 {{name}}", moveTitle: "移动 {{name}}", propertiesTitle: "属性 — {{name}}", mode: "模式", linkTarget: "链接目标", newName: "新名称", deleteTitle: "删除 {{name}}？", deleteBody: "此操作仅作用于选中的远程条目。", confirmDelete: "确认删除", recursiveTitle: "确认递归删除", recursiveSummary: "{{path}} 下有 {{files}} 个文件、{{directories}} 个目录、{{links}} 个符号链接，共 {{bytes}} 字节。", confirmRecursive: "递归删除", transfer: "传输", cancelOperation: "取消操作", recoveries: "恢复中心", noRecoveries: "当前没有待恢复操作。", verify: "验证结果", executeRecovery: "执行恢复", viaSymlink: "经由符号链接", disconnectTransferTitle: "SFTP 传输仍在进行", disconnectTransferBody: "此 SSH 会话仍拥有用户手动 SFTP 传输。请继续等待，或取消传输并等待清理完成后再断开。", disconnectCommittingBody: "传输正在提交，已不能安全取消。请继续等待。", disconnectRecoveryBody: "清理未能完成。断开前请保留受保护的恢复记录。", keepRecoveryAndDisconnect: "保留恢复记录并断开", recoveryConfirmTitle: "确认恢复操作", recoveryConfirmBody: "{{action}} 会更改保留的恢复状态，需要再次明确确认。", confirmRecoveryAction: "确认恢复操作", recoveryStates: { cleanup_required: "需要清理", outcome_unknown: "结果未知", recovery_required: "需要恢复" }, recoveryActions: { deleteTemp: "删除临时文件", continueDelete: "继续删除", restoreTombstone: "恢复隔离项", keep: "稍后处理" }, entryTypes: { file: "文件", directory: "目录", symlink: "符号链接", other: "其他" }, transferPhases: { preparing: "准备中", transferring: "传输中", verifying: "校验中", committing: "提交中" } },
  agent: { title: "Agent", unavailable: "M2 尚未启用 Agent", expand: "展开 Agent 说明", collapse: "折叠 Agent 说明", boundary: "Agent channel 与此人工 PTY 保持隔离。", unavailableTitle: "Agent 将在 M3 提供", unavailableBody: "当前 M2 工作区不执行 Agent 任务。", message: "消息", messagePlaceholder: "让 Agent 检查或操作当前 SSH 会话…", provider: "Provider", chooseProvider: "选择 Provider", providerSettings: "Provider 设置", openProviderSettings: "打开 Provider 设置", send: "发送消息", enterToSend: "Enter 发送 · Shift+Enter 换行", running: "正在接收回答…", thinking: "思考中…", newConversation: "新对话", resetTitle: "开始新对话？", resetBody: "这只会清除当前标签内存中的对话视图。", confirmReset: "开始新对话", riskTitle: "允许实验性远程命令执行？", riskBody: "Agent 可以在当前 SSH Session 执行 Shell 命令。命令可能修改远程主机；当前没有逐条审批、可靠的前端停止能力或生产安全保证。", confirmRisk: "确认风险并发送", compactRisk: "实验性 Agent：命令可能修改远程主机，且无法从当前 UI 可靠停止。", emptySession: "请选择一个已连接的终端标签以使用 Agent。", emptyProvider: "发送前请配置并选择已启用的 Provider。", runDetails: "Run 详情", sentSnapshot: "本轮发送快照", runId: "Run ID", runStatus: "状态", iteration: "迭代次数", session: "会话", apiType: "API 类型", model: "模型", noMessages: "当前标签还没有消息。", completedAnnouncement: "Agent 已为 {{name}} 完成", failedAnnouncement: "Agent 在 {{name}} 上失败", tabRunning: "Agent 正在为 {{name}} 运行", tabCompleted: "Agent 已为 {{name}} 完成", tabFailed: "Agent 在 {{name}} 上失败", activeRunTitle: "Agent Run 仍在运行", activeRunBody: "{{name}} 仍有一个活动 Agent Run。" },
  settings: { language: "语言", general: "常规", close: "关闭设置", diagnostics: { title: "诊断", description: "Harness Shell 会将本地应用和 Agent 日志写入此处。", loading: "正在检查日志目录…", available: "日志目录可用。", open: "打开日志目录", opening: "正在打开…" }, modelProviders: { title: "模型 Provider", newProvider: "新建 Provider", empty: "尚未配置模型 Provider。", loading: "正在加载 Provider…", retry: "重试", enabled: "已启用", disabled: "已禁用", storedKey: "API Key 已安全保存", activeRun: "正在被活动 Run 使用", createTitle: "新建 Provider", editTitle: "编辑 Provider", deleteTitle: "删除 Provider？", deleteBody: "删除 {{name}} 及其已保存的 API Key？", confirmDelete: "确认删除", displayName: "显示名称", apiType: "API 类型", baseUrl: "Base URL", model: "模型", apiKey: "API Key", keepCurrentKey: "留空以保留当前 API Key", required: "必填", invalid: "值无效", save: "保存", primaryFailure: "Provider 操作失败" } },
  language: { system: "跟随系统", zhCN: "简体中文", zhTW: "繁體中文", en: "English" },
  status: { runtime: "Runtime", ssh: "SSH", hostKey: "Host Key", pty: "PTY 尺寸", agent: "Agent", route: "路由" },
  hostKey: { identity: "主机身份", changed: "Host Key 已变更", trust: "信任此主机？", changedBody: "替换可信密钥前，请通过独立可信渠道核对两个指纹。", trustBody: "连接前，请通过可信渠道核对此指纹。", algorithm: "算法", trustedFingerprint: "可信指纹", newFingerprint: "新指纹", fingerprint: "SHA-256 指纹", replace: "替换可信密钥", trustConnect: "信任并连接" },
  errors: { sshFailed: "SSH 操作失败", hostKeyConflict: "可信 Host Key 再次发生变化", profileSavedConnectFailed: "配置已保存；连接失败", technicalDetails: "技术详情", whatNext: "请根据错误原因修正配置后再重试。", retry: "重试", edit: "编辑连接" },
  runtime: { failedTitle: "Runtime 不可用", failedBody: "本地 Runtime 不可用期间，交互操作已禁用。", retryStatus: "重新检查 Runtime" },
};

const zhTW = {
  common: { appName: "Harness Shell", cancel: "取消", save: "儲存", saveAndConnect: "儲存並連線", delete: "刪除", close: "關閉", copyDetails: "複製詳情", copied: "已複製" },
  nav: { connections: "SSH 連線", files: "檔案", sftp: "SFTP", settings: "設定", agent: "AI Agent", milestone: "將在 {{milestone}} 提供" },
  shell: { primaryActivities: "主要活動", resizeSidebar: "調整連線側欄寬度", resizeAgent: "調整 Agent 工作區寬度" },
  applicationClose: { title: "退出 Harness Shell？", body: "關閉程式將結束目前工作區和所有工作階段。", confirm: "退出 Harness Shell", activeTransferBody: "仍有使用者手動 SFTP 傳輸正在進行。請繼續等待，或取消傳輸並等待清理完成後再退出。", committingBody: "使用者手動 SFTP 傳輸正在提交，已無法安全取消。請繼續等待傳輸完成。", recoveryBody: "傳輸清理未能完成。受保護的復原記錄會保留到下次啟動。", continueWaiting: "繼續等待", cancelAndCleanUp: "取消並清理", keepRecoveryAndClose: "保留復原記錄並關閉", activeAgentBody: "仍有 {{count}} 個 Agent Run 正在執行。退出後遠端結果可能未知。", forceExit: "強制退出" },
  topbar: { noConnection: "未選擇連線", toggleSidebar: "切換連線側欄", language: "語言", followSystem: "跟隨系統", terminal: "聚焦終端", quickActions: "快捷操作", newConnection: "新增連線", editConnection: "編輯連線", focusTerminal: "聚焦使用中終端", localEnvironment: "本機" },
  activity: { settings: "設定", filesUnavailable: "檔案功能預計於 M3 提供" },
  connections: { title: "連線", search: "搜尋主機、名稱或群組", new: "新增連線", actions: "連線操作", open: "開啟連線", edit: "編輯連線", connect: "連線", disconnect: "中斷", reconnect: "重新連線", noResults: "沒有符合的連線", ungrouped: "未分組", favorite: "收藏", basic: "基本資訊", authentication: "身分驗證", advanced: "進階設定", displayName: "連線名稱", group: "群組", host: "主機", port: "連接埠", username: "使用者名稱", authMethod: "驗證方式", password: "密碼", privateKey: "私鑰", passphrase: "私鑰密語", proxyJump: "ProxyJump", direct: "直接連線", importKey: "匯入私鑰…", keySelected: "已選擇私鑰", required: "必填", invalid: "數值無效", keepCurrent: "留空以保留現有秘密", hostKeyStatus: "Host Key", deleteConfirmTitle: "刪除此連線？", deleteConfirmBody: "只會刪除本機連線設定，不影響遠端主機。", confirmDelete: "確認刪除", savedConnectFailed: "設定已儲存，但連線失敗。" },
  terminal: { title: "互動式終端", runtimeReady: "Runtime ready", inputDisabled: "輸入已停用", emptyTitle: "尚未開啟終端", emptyBody: "請選擇連線或新增連線設定。", selectConnection: "選擇連線", createConnection: "新增連線", closeTab: "關閉 {{name}}", humanBoundary: "人工 PTY — 與 Agent 輸入隔離", sidecarUnavailable: "Sidecar 無法使用。現有 PTY 已中斷；未建立替代終端。", actions: "工作階段操作", reconnect: "重新連線", disconnect: "中斷連線", closeConfirmTitle: "關閉工作階段？", closeConnectedBody: "分頁會立即關閉。Harness Shell 將在背景關閉 PTY 並中斷 SSH。", closeLocalBody: "這將移除本機分頁及保留的終端輸出。", confirmClose: "關閉工作階段", reconnectDivider: "── 已重新連線 ──", cleanupFailed: "未能完成 {{name}} 的清理", retryCleanup: "重試清理", cleanupRetrying: "正在重試清理…", states: { connecting: "正在連線", hostKeyRequired: "等待 Host Key", connected: "已連線", disconnecting: "正在中斷", disconnected: "已中斷", failed: "連線失敗" } },
  sftp: { title: "遠端檔案", noSessionTitle: "未選擇已連線的終端", noSessionBody: "使用 SFTP 前，請開啟或選擇一個已連線的終端分頁。", selectConnection: "選擇連線", loadingRemoteFiles: "正在載入遠端檔案…", loadingDirectory: "正在載入 {{path}}…", loadingTreeDirectory: "正在載入 {{path}} 中的目錄…", loadingProperties: "正在讀取 {{name}} 的屬性…", calculatingHash: "正在計算 {{name}} 的 SHA-256…", resolvingLink: "正在解析 {{name}}…", loadingRecoveries: "正在載入復原記錄…", path: "遠端路徑", go: "前往", parent: "上層目錄", refresh: "重新整理", upload: "上傳檔案", download: "下載", newFolder: "新增資料夾", rename: "重新命名", move: "移動", properties: "屬性", open: "開啟", openTarget: "開啟目標", readLinkTarget: "讀取連結目標", delete: "刪除", name: "名稱", size: "大小", type: "類型", modified: "修改時間", actions: "操作", empty: "此目錄為空。", tree: "遠端目錄樹", uploadNameTitle: "上傳檔案", targetName: "遠端檔名", targetPath: "目標路徑", continue: "繼續", transferTitle: "確認{{direction}}？", confirmTransfer: "開始傳輸", confirmRenameOverwrite: "確認覆寫", overwriteWarning: "目標已存在，將使用原子操作取代。", externalRace: "遠端未加鎖。提交視窗內，其他用戶端對同一路徑的更新可能被最終原子重新命名取代。", mkdirTitle: "新增目錄", renameTitle: "重新命名 {{name}}", moveTitle: "移動 {{name}}", propertiesTitle: "屬性 — {{name}}", mode: "模式", linkTarget: "連結目標", newName: "新名稱", deleteTitle: "刪除 {{name}}？", deleteBody: "此操作只會作用於選取的遠端項目。", confirmDelete: "確認刪除", recursiveTitle: "確認遞迴刪除", recursiveSummary: "{{path}} 下有 {{files}} 個檔案、{{directories}} 個目錄、{{links}} 個符號連結，共 {{bytes}} 位元組。", confirmRecursive: "遞迴刪除", transfer: "傳輸", cancelOperation: "取消操作", recoveries: "復原中心", noRecoveries: "目前沒有待復原操作。", verify: "驗證結果", executeRecovery: "執行復原", viaSymlink: "經由符號連結", disconnectTransferTitle: "SFTP 傳輸仍在進行", disconnectTransferBody: "此 SSH 工作階段仍擁有使用者手動 SFTP 傳輸。請繼續等待，或取消傳輸並等待清理完成後再中斷。", disconnectCommittingBody: "傳輸正在提交，已無法安全取消。請繼續等待。", disconnectRecoveryBody: "清理未能完成。中斷前請保留受保護的復原記錄。", keepRecoveryAndDisconnect: "保留復原記錄並中斷", recoveryConfirmTitle: "確認復原操作", recoveryConfirmBody: "{{action}} 會變更保留的復原狀態，需要再次明確確認。", confirmRecoveryAction: "確認復原操作", recoveryStates: { cleanup_required: "需要清理", outcome_unknown: "結果未知", recovery_required: "需要復原" }, recoveryActions: { deleteTemp: "刪除暫存檔", continueDelete: "繼續刪除", restoreTombstone: "復原隔離項", keep: "稍後處理" }, entryTypes: { file: "檔案", directory: "目錄", symlink: "符號連結", other: "其他" }, transferPhases: { preparing: "準備中", transferring: "傳輸中", verifying: "驗證中", committing: "提交中" } },
  agent: { title: "Agent", unavailable: "M2 尚未啟用 Agent", expand: "展開 Agent 說明", collapse: "收合 Agent 說明", boundary: "Agent channel 與此人工 PTY 保持隔離。", unavailableTitle: "Agent 將於 M3 提供", unavailableBody: "目前 M2 工作區不執行 Agent 任務。", message: "訊息", messagePlaceholder: "讓 Agent 檢查或操作目前 SSH 工作階段…", provider: "Provider", chooseProvider: "選擇 Provider", providerSettings: "Provider 設定", openProviderSettings: "開啟 Provider 設定", send: "傳送訊息", enterToSend: "Enter 傳送 · Shift+Enter 換行", running: "正在接收回答…", thinking: "思考中…", newConversation: "新對話", resetTitle: "開始新對話？", resetBody: "這只會清除目前分頁記憶體中的對話檢視。", confirmReset: "開始新對話", riskTitle: "允許實驗性遠端命令執行？", riskBody: "Agent 可以在目前 SSH Session 執行 Shell 命令。命令可能修改遠端主機；目前沒有逐條審批、可靠的前端停止能力或正式環境安全保證。", confirmRisk: "確認風險並傳送", compactRisk: "實驗性 Agent：命令可能修改遠端主機，且無法從目前 UI 可靠停止。", emptySession: "請選擇一個已連線的終端分頁以使用 Agent。", emptyProvider: "傳送前請設定並選擇已啟用的 Provider。", runDetails: "Run 詳情", sentSnapshot: "本輪傳送快照", runId: "Run ID", runStatus: "狀態", iteration: "迭代次數", session: "工作階段", apiType: "API 類型", model: "模型", noMessages: "目前分頁還沒有訊息。", completedAnnouncement: "Agent 已為 {{name}} 完成", failedAnnouncement: "Agent 在 {{name}} 上失敗", tabRunning: "Agent 正在為 {{name}} 執行", tabCompleted: "Agent 已為 {{name}} 完成", tabFailed: "Agent 在 {{name}} 上失敗", activeRunTitle: "Agent Run 仍在執行", activeRunBody: "{{name}} 仍有一個作用中的 Agent Run。" },
  settings: { language: "語言", general: "一般", close: "關閉設定", diagnostics: { title: "診斷", description: "Harness Shell 會將本機應用程式和 Agent 日誌寫入此處。", loading: "正在檢查日誌目錄…", available: "日誌目錄可用。", open: "開啟日誌目錄", opening: "正在開啟…" }, modelProviders: { title: "模型 Provider", newProvider: "新增 Provider", empty: "尚未設定模型 Provider。", loading: "正在載入 Provider…", retry: "重試", enabled: "已啟用", disabled: "已停用", storedKey: "API Key 已安全儲存", activeRun: "正在被作用中的 Run 使用", createTitle: "新增 Provider", editTitle: "編輯 Provider", deleteTitle: "刪除 Provider？", deleteBody: "刪除 {{name}} 及其已儲存的 API Key？", confirmDelete: "確認刪除", displayName: "顯示名稱", apiType: "API 類型", baseUrl: "Base URL", model: "模型", apiKey: "API Key", keepCurrentKey: "留空以保留目前 API Key", required: "必填", invalid: "數值無效", save: "儲存", primaryFailure: "Provider 操作失敗" } },
  language: { system: "跟隨系統", zhCN: "简体中文", zhTW: "繁體中文", en: "English" },
  status: { runtime: "Runtime", ssh: "SSH", hostKey: "Host Key", pty: "PTY 尺寸", agent: "Agent", route: "路由" },
  hostKey: { identity: "主機身分", changed: "Host Key 已變更", trust: "信任此主機？", changedBody: "更換可信金鑰前，請透過獨立可信管道核對兩個指紋。", trustBody: "連線前，請透過可信管道核對這個指紋。", algorithm: "演算法", trustedFingerprint: "可信指紋", newFingerprint: "新指紋", fingerprint: "SHA-256 指紋", replace: "更換可信金鑰", trustConnect: "信任並連線" },
  errors: { sshFailed: "SSH 操作失敗", hostKeyConflict: "可信 Host Key 再次發生變更", profileSavedConnectFailed: "設定已儲存；連線失敗", technicalDetails: "技術詳情", whatNext: "請依錯誤原因修正設定後再重試。", retry: "重試", edit: "編輯連線" },
  runtime: { failedTitle: "Runtime 無法使用", failedBody: "本機 Runtime 無法使用期間，互動操作已停用。", retryStatus: "重新檢查 Runtime" },
};

export const resources = {
  en: { translation: en },
  "zh-CN": { translation: zhCN },
  "zh-TW": { translation: zhTW },
} as const;

export const flattenResourceKeys = (value: object, prefix = ""): string[] =>
  Object.entries(value)
    .flatMap(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      return typeof child === "object" && child !== null
        ? flattenResourceKeys(child, path)
        : [path];
    })
    .sort();
