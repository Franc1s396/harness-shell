const en = {
  common: {
    appName: "Harness Shell", approval: "Approval", cancel: "Cancel", save: "Save",
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
  },
  topbar: {
    noConnection: "No connection selected", toggleSidebar: "Toggle connection sidebar",
    language: "Language", followSystem: "Follow system", terminal: "Focus terminal",
  },
  connections: {
    title: "Connections", search: "Search host, name, or group", new: "New connection",
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
  },
  agent: {
    title: "AI Agent", unavailable: "Agent is not enabled in M2",
    expand: "Expand Agent information", collapse: "Collapse Agent information",
    boundary: "The Agent channel remains isolated from this human PTY.",
  },
  language: { system: "Follow system", zhCN: "简体中文", zhTW: "繁體中文", en: "English" },
  status: { runtime: "Runtime", ssh: "SSH", hostKey: "Host Key", pty: "PTY size", route: "Route" },
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
  },
  approval: {
    eyebrow: "Security & approval", title: "Approval requests",
    subtitle: "Sensitive actions require an explicit decision in this window.",
    unavailable: "Approval context is unavailable.", empty: "No approval request is pending.", loading: "Loading…",
  },
};

const zhCN = {
  common: { appName: "Harness Shell", approval: "审批", cancel: "取消", save: "保存", saveAndConnect: "保存并连接", delete: "删除", close: "关闭", copyDetails: "复制详情", copied: "已复制" },
  nav: { connections: "SSH 连接", files: "文件", sftp: "SFTP", settings: "设置", agent: "AI Agent", milestone: "将在 {{milestone}} 提供" },
  shell: { primaryActivities: "主要活动", resizeSidebar: "调整连接侧栏宽度" },
  topbar: { noConnection: "未选择连接", toggleSidebar: "切换连接侧栏", language: "语言", followSystem: "跟随系统", terminal: "聚焦终端" },
  connections: { title: "连接", search: "搜索主机、名称或分组", new: "新建连接", edit: "编辑连接", connect: "连接", disconnect: "断开", reconnect: "重新连接", noResults: "没有匹配的连接", ungrouped: "未分组", favorite: "收藏", basic: "基础信息", authentication: "身份认证", advanced: "高级设置", displayName: "连接名称", group: "分组", host: "主机", port: "端口", username: "用户名", authMethod: "认证方式", password: "密码", privateKey: "私钥", passphrase: "私钥口令", proxyJump: "ProxyJump", direct: "直连", importKey: "导入私钥…", keySelected: "已选择私钥", required: "必填", invalid: "值无效", keepCurrent: "留空以保留现有秘密", hostKeyStatus: "Host Key", deleteConfirmTitle: "删除此连接？", deleteConfirmBody: "只删除本地连接配置，不影响远端主机。", confirmDelete: "确认删除", savedConnectFailed: "配置已保存，但连接失败。" },
  terminal: { title: "交互式终端", runtimeReady: "Runtime ready", inputDisabled: "输入已禁用", emptyTitle: "尚未打开终端", emptyBody: "请选择连接或新建连接配置。", selectConnection: "选择连接", createConnection: "新建连接", closeTab: "关闭 {{name}}", humanBoundary: "人工 PTY — 与 Agent 输入隔离", sidecarUnavailable: "Sidecar 不可用。现有 PTY 已断开；未创建兜底终端。" },
  agent: { title: "AI Agent", unavailable: "M2 尚未启用 Agent", expand: "展开 Agent 说明", collapse: "折叠 Agent 说明", boundary: "Agent channel 与此人工 PTY 保持隔离。" },
  language: { system: "跟随系统", zhCN: "简体中文", zhTW: "繁體中文", en: "English" },
  status: { runtime: "Runtime", ssh: "SSH", hostKey: "Host Key", pty: "PTY 尺寸", route: "路由" },
  hostKey: { identity: "主机身份", changed: "Host Key 已变更", trust: "信任此主机？", changedBody: "替换可信密钥前，请通过独立可信渠道核对两个指纹。", trustBody: "连接前，请通过可信渠道核对此指纹。", algorithm: "算法", trustedFingerprint: "可信指纹", newFingerprint: "新指纹", fingerprint: "SHA-256 指纹", replace: "替换可信密钥", trustConnect: "信任并连接" },
  errors: { sshFailed: "SSH 操作失败", hostKeyConflict: "可信 Host Key 再次发生变化", profileSavedConnectFailed: "配置已保存；连接失败", technicalDetails: "技术详情", whatNext: "请根据错误原因修正配置后再重试。" },
  approval: { eyebrow: "安全与审批", title: "审批请求", subtitle: "敏感操作必须在此独立窗口中明确确认。", unavailable: "无法获取审批上下文。", empty: "当前没有待处理的审批请求。", loading: "加载中…" },
};

const zhTW = {
  common: { appName: "Harness Shell", approval: "審批", cancel: "取消", save: "儲存", saveAndConnect: "儲存並連線", delete: "刪除", close: "關閉", copyDetails: "複製詳情", copied: "已複製" },
  nav: { connections: "SSH 連線", files: "檔案", sftp: "SFTP", settings: "設定", agent: "AI Agent", milestone: "將在 {{milestone}} 提供" },
  shell: { primaryActivities: "主要活動", resizeSidebar: "調整連線側欄寬度" },
  topbar: { noConnection: "未選擇連線", toggleSidebar: "切換連線側欄", language: "語言", followSystem: "跟隨系統", terminal: "聚焦終端" },
  connections: { title: "連線", search: "搜尋主機、名稱或群組", new: "新增連線", edit: "編輯連線", connect: "連線", disconnect: "中斷", reconnect: "重新連線", noResults: "沒有符合的連線", ungrouped: "未分組", favorite: "收藏", basic: "基本資訊", authentication: "身分驗證", advanced: "進階設定", displayName: "連線名稱", group: "群組", host: "主機", port: "連接埠", username: "使用者名稱", authMethod: "驗證方式", password: "密碼", privateKey: "私鑰", passphrase: "私鑰密語", proxyJump: "ProxyJump", direct: "直接連線", importKey: "匯入私鑰…", keySelected: "已選擇私鑰", required: "必填", invalid: "數值無效", keepCurrent: "留空以保留現有秘密", hostKeyStatus: "Host Key", deleteConfirmTitle: "刪除此連線？", deleteConfirmBody: "只會刪除本機連線設定，不影響遠端主機。", confirmDelete: "確認刪除", savedConnectFailed: "設定已儲存，但連線失敗。" },
  terminal: { title: "互動式終端", runtimeReady: "Runtime ready", inputDisabled: "輸入已停用", emptyTitle: "尚未開啟終端", emptyBody: "請選擇連線或新增連線設定。", selectConnection: "選擇連線", createConnection: "新增連線", closeTab: "關閉 {{name}}", humanBoundary: "人工 PTY — 與 Agent 輸入隔離", sidecarUnavailable: "Sidecar 無法使用。現有 PTY 已中斷；未建立替代終端。" },
  agent: { title: "AI Agent", unavailable: "M2 尚未啟用 Agent", expand: "展開 Agent 說明", collapse: "收合 Agent 說明", boundary: "Agent channel 與此人工 PTY 保持隔離。" },
  language: { system: "跟隨系統", zhCN: "简体中文", zhTW: "繁體中文", en: "English" },
  status: { runtime: "Runtime", ssh: "SSH", hostKey: "Host Key", pty: "PTY 尺寸", route: "路由" },
  hostKey: { identity: "主機身分", changed: "Host Key 已變更", trust: "信任此主機？", changedBody: "更換可信金鑰前，請透過獨立可信管道核對兩個指紋。", trustBody: "連線前，請透過可信管道核對這個指紋。", algorithm: "演算法", trustedFingerprint: "可信指紋", newFingerprint: "新指紋", fingerprint: "SHA-256 指紋", replace: "更換可信金鑰", trustConnect: "信任並連線" },
  errors: { sshFailed: "SSH 操作失敗", hostKeyConflict: "可信 Host Key 再次發生變更", profileSavedConnectFailed: "設定已儲存；連線失敗", technicalDetails: "技術詳情", whatNext: "請依錯誤原因修正設定後再重試。" },
  approval: { eyebrow: "安全與審批", title: "審批請求", subtitle: "敏感操作必須在此獨立視窗中明確確認。", unavailable: "無法取得審批內容。", empty: "目前沒有待處理的審批請求。", loading: "載入中…" },
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
