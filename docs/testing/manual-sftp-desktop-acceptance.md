# 用户手动 SFTP 文件管理器桌面验收

> 本清单已按当前 Launcher + React direct Backend 架构更新；任何旧 Rust 本地文件 owner 或可恢复本地下载状态均不再适用。

## 证据边界

本清单只记录当前 Windows checkout 的 Tauri Desktop 人工观察。自动门禁、containerized OpenSSH Lab、桌面人工验收和 production host/deployment/migration 验收是不同证据层级，不能相互替代。

用户手动 SFTP 只允许人类在 SFTP Activity 中显式操作。Agent、M3 Workflow 和 WebView raw Protocol 均无 SFTP/exec 路由；当前也不存在独立 approval UI。失败后不得自动联网、恢复或重放 mutation。

## 环境与构建身份

- 验收日期：未执行
- 验收人：未执行
- Windows 版本：未记录
- Git branch：`codex/manual-sftp-file-manager`
- Git commit / dirty tree 摘要：未记录
- Node/npm：未记录
- Python：未记录
- Rust/Tauri：未记录
- Sidecar `.exe` SHA-256：未记录
- 测试主机/容器身份与 Host Key 指纹：未记录
- 自动门禁结果：未记录

## 启动

从 `frontend/` 运行：

```powershell
npm run tauri:dev
```

## 人工操作清单

### Session 绑定与布局

- [ ] 没有 active connected terminal tab 时进入 SFTP，界面明确要求选择/建立连接，不猜测其它 tab。
- [ ] 同时打开两个已连接 tab，激活第一个后进入 SFTP；host/path/listing 只来自第一个 tab。
- [ ] 保持在 SFTP Activity 内切换普通 active tab，SFTP 仍固定绑定进入时的第一个 Session，不迁移 listing 或 transfer owner；离开 SFTP、选中第二个 tab 后重新进入，才绑定第二个 Session，两个 Session 的 last path 互不覆盖。
- [ ] 进入 SFTP 时 Agent pane 与 collapsed Agent button 都隐藏；返回 Terminal 后恢复先前 Agent visibility 偏好。
- [ ] 900×600 下 remote tree 隐藏，但 path、Parent、toolbar 和 file table 可用；不存在 CSS scale/transform 缩放。

### Native picker 与基本文件操作

- [ ] Upload 打开 Windows native Open dialog；取消后无远程 mutation、无错误形状的成功结果。
- [ ] Download 打开 Windows native Save As dialog；取消后无本地 `.part`、无远程 mutation。
- [ ] 上传零字节、UTF-8/中文/Emoji/空格名称和较大文件，界面显示 host、remote path、bytes、phase。
- [ ] 下载上述文件并核对完整字节与 mandatory SHA-256。
- [ ] 新建目录、重命名/同文件系统移动、删除普通文件、删除空目录均要求显式用户动作并成功刷新 listing。
- [ ] file/directory/symlink 各自只显示批准的单项操作；Move 走 rename，Properties 显示 no-follow metadata，普通文件 SHA-256 和 symlink link target 仅在显式点击后读取。无 batch、drag/drop、directory merge 或 recursive upload/download。
- [ ] remote tree 首次展开节点时才读取该目录，支持展开/折叠与导航；900×600 隐藏 tree 后 path、Parent 和 table 仍可操作。
- [ ] 非空目录先显示 file/directory/symlink count、total bytes、完整 root path 与 manifest SHA-256，再经过第二次确认执行 tombstone delete。
- [ ] 同一路径覆盖确认显示 display name、remote path、size、SHA-256；upload 还明确提示无 remote lock 及 recheck-to-rename TOCTOU 边界。
- [ ] 在确认后、commit 前用另一个客户端修改目标；应用检测 `SFTP_TARGET_CHANGED`，不覆盖外部更新。
- [ ] 记录已接受边界：纯 SFTP 无法排除外部客户端在最后一次 recheck 与 atomic rename 之间写入同一路径。

### Symlink、键盘与焦点

- [ ] listing 将 symlink 显示为 link，不跟随进入 target；显式 Open Link 才解析 target。
- [ ] 删除 symlink 只删除 link，target 文件仍存在。
- [ ] file grid 支持 ArrowUp/ArrowDown、Enter、Backspace、F2、Delete 与 Ctrl+R。
- [ ] Delete/F2/确认框关闭后焦点返回原 grid/触发控件；键盘操作不会在 input 获得焦点时错误触发 Backspace parent。
- [ ] Activity 入口、grid rows、dialog title/button 有可辨识 accessible name。

### Locale、取消、关闭与恢复

- [ ] `zh-CN`、`zh-TW`、`en` 三个 locale 的 SFTP 文案完整，无 key fallback。
- [ ] transfer 只有在 `cancellable=true` 时显示 Cancel；`committing` 和 mutation 不显示可取消承诺。
- [ ] 关闭 dialog 会 discard 未执行 preparation；离开 SFTP/切换 Session 会关闭 listing 并清理 preparation。
- [ ] 中断 upload/download 后，没有可信 terminal 时 Recovery Center 显示 recovery entry，不自动执行网络连接或 mutation。
- [ ] 应用重启后，本地 download `.part` Recovery Center 使用加密记录中的安全 host label 和目标 basename；Inspect/Keep/Open local folder 不连接 Python，DOM 不出现本地绝对路径。
- [ ] `OUTCOME_UNKNOWN` 只提供 Verify Result；mutating recovery 必须再次显式确认并使用 fresh operation ID。
- [ ] `CLEANUP_REQUIRED` 自动打开 Recovery Center；失败、取消与关闭路径均显示稳定安全 error code/message。
- [ ] active pre-commit transfer 上请求 Disconnect 或关闭应用，先显示 Continue waiting / Cancel and clean up，决策前不先断连或关闭；committing 只能等待，cleanup 失败后才显示 Keep recovery record and close。

### DevTools 敏感数据检查

- [ ] DOM、React props/state、console、Tauri event payload 和 error text 不出现 Windows 本地绝对路径。
- [ ] DOM、console、event、SQLite 明文和错误中不出现上传/下载文件内容 marker。
- [ ] UI store、日志和错误详情不可见 credential、Backend stderr 或 native picker 返回路径；React 只在当前页面内持有 File/System Access handle，Python 不接收本地绝对路径。
- [ ] `manual-sftp://operation-state` 与 `manual-sftp://transfer-state` 只包含批准的 typed projection 字段。

## 结果

- Tauri Desktop 人工验收：未执行，等待用户逐项观察并确认。
- Production host 验收：未执行。
- Production deployment 验收：未执行。
- Production migration 验收：未执行。
- 破坏性 production fault injection：未执行，且本清单不授权执行。
